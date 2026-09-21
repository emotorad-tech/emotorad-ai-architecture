# Media Storage on S3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every image and video the platform stores or serves lives in one private S3 bucket per environment; customers and staff upload through the same presigned-URL flow; reads are short-lived presigned GETs; the production agent loop can see attachments; guide media can leave Cloudinary.

**Architecture:** Three small modules under `storage/` (`keys.py` pure key derivation, `s3.py` the only boto3 user, `uploads.py` the presign→attach registry). A shared `attachments.py` turns any `Attachment` (data URL or `s3://` key) into Claude content blocks, with the video frame and transcription helpers moved out of the playground into `video.py`. `api.py` gains `POST /uploads` and `GET /media/{key}` and accepts `attachments` on `/message`. `media.resolve()` learns S3 ids (with a slash) beside Cloudinary ids (without). CloudFormation creates the bucket and the role policy; a runbook covers staff uploads, the curl walkthrough, and the Cloudinary migration.

**Tech Stack:** Python 3.12, `unittest`, `boto3` + `botocore.stub.Stubber`, Pillow, imageio-ffmpeg, FastAPI + `TestClient`, CloudFormation. Spec: `docs/superpowers/specs/2026-09-21-media-storage-design.md`.

## Global Constraints

- Repo `~/emotorad/emotorad-ai-architecture`, branch `feat/media-storage` (cut from `feat/aws-secrets`). 4-space indentation, `from __future__ import annotations`, docstrings explain *why*.
- Tests: `.venv/bin/python3 -m unittest discover -s tests -t .` must stay green (511 today, pristine output). `pyflakes` gate in the suite covers `src/emotorad_ai`, `scripts`, `docker`.
- Bucket per environment `emotorad-ai-<env>-media`, `ap-south-1`; env var `EMOTORAD_AI_MEDIA_BUCKET`; unset means uploads disabled (`/uploads` and `/media` answer 503 with a reason) and the playground keeps local blobs.
- Key layout, exactly: `assets/<programme>/<category>/<kind>/<slug>.<ext>` with derivatives `<slug>.w900.webp` (images) and `<slug>.poster.jpg` (videos); `customers/<cluster_id>/<conversation_id>/<kind>/<upload_id>.<ext>`. `programme ∈ {afs, presales, dealer}`; asset `kind ∈ {photos, videos, tips, docs}`; customer `kind ∈ {images, videos, docs}`; `category` and `slug` match `^[a-z0-9][a-z0-9-]*$`. The extension comes from the MIME type, never the file name. A phone number never appears in a key.
- MIME allowlist: `image/jpeg`, `image/png`, `image/webp`, `video/mp4`, `application/pdf`. Size caps: images 10 MB, videos 100 MB, docs 10 MB. Presigned PUT pins `Content-Type` and `Content-Length`; PUT expiry 300 s; GET expiry 900 s.
- The client never chooses a key. The API never proxies upload bytes. No URL of any kind reaches the model; evidence is fetched server-side and sent as base64.
- **Spec adjustments recorded here:** a record `id` includes the extension (`afs/battery/photos/soc-button.jpg`) so `resolve()` needs no S3 lookup; an id containing `/` is an S3 key under `assets/`, an id without `/` is a Cloudinary public id and keeps resolving while `EMOTORAD_CLOUDINARY_CLOUD` is set.
- No secret values in code, logs, tests or commits. `*secret*`/`*credential*` paths are gitignored.
- AWS CLI: `--profile emotorad-staging --region ap-south-1`, account `851725486214`, instance role `emotorad-ai-stage-ec2-role`. Read-only commands may run in tasks; `cloudformation deploy` and any write only in the final task, by the controller.
- Commit after every task, conventional-commit subject, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Never push without the person's yes.

---

## File structure

| File | Responsibility |
|---|---|
| `src/emotorad_ai/storage/__init__.py`, `keys.py` (new) | vocabularies, MIME→ext, key derivation, validation; pure |
| `src/emotorad_ai/storage/s3.py` (new) | `S3Store`: presign put/get, head, get/put bytes; `store_from_env()` |
| `src/emotorad_ai/storage/uploads.py` (new) | `UploadRegistry`: begin (presign) → claim (verify) |
| `src/emotorad_ai/video.py` (new), `playground.py` (modify) | frame sampling, audio extraction, transcription moved out; playground re-exports |
| `src/emotorad_ai/attachments.py` (new) | `content_blocks(attachments, fetch)`: data URLs and `s3://` keys → Claude blocks |
| `src/emotorad_ai/agents/base.py`, `runtime.py` (modify) | inbound attachments reach the model; `Runtime(media_store=...)` |
| `src/emotorad_ai/media.py` (modify) | `resolve()` handles S3 ids and derivatives |
| `src/emotorad_ai/api.py` (modify) | `POST /uploads`, `GET /media/{key}`, `attachments` on `MessageIn` |
| `src/emotorad_ai/playground.py` (modify) | blobs go to S3 under `customers/playground/<chat_id>/` when configured |
| `scripts/upload_asset.py`, `scripts/migrate_cloudinary.py` (new) | staff loading with derivatives; one-time move |
| `infra/media.yaml`, `docs/runbooks/media.md` (new); `.github/workflows/deploy-staging.yml`, `requirements.txt`, `README.md` (modify) | bucket, policy, runbook, env var, Pillow |

---

### Task 1: `storage/keys.py`

**Files:**
- Create: `src/emotorad_ai/storage/__init__.py` (empty docstring module), `src/emotorad_ai/storage/keys.py`
- Test: `tests/test_storage_keys.py`

**Interfaces:**
- Produces: `PROGRAMMES = ("afs", "presales", "dealer")`, `ASSET_KINDS = ("photos", "videos", "tips", "docs")`, `CUSTOMER_KINDS = ("images", "videos", "docs")`, `MIME_TYPES: Dict[str, str]` (mime → ext), `SIZE_CAPS: Dict[str, int]` (customer kind → bytes), `KeyError_ = KeyValidationError(ValueError)`, `extension_for(mime) -> str`, `customer_kind_for(mime) -> str`, `asset_key(programme, category, kind, slug, mime) -> str`, `derivative_keys(asset_key) -> Dict[str, str]` (`{"w900": ..., "poster": ...}` as applicable), `customer_key(cluster_id, conversation_id, kind, upload_id, mime) -> str`, `new_upload_id() -> str` (time-sortable, `upl_` prefix), `is_customer_key(key)`, `is_asset_key(key)`, `cluster_of(customer_key) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_storage_keys.py
"""Keys are derived by code from closed vocabularies. The client never chooses
one, and a phone number can never end up in one."""

import re
import unittest

from emotorad_ai.storage.keys import (
    ASSET_KINDS,
    CUSTOMER_KINDS,
    PROGRAMMES,
    SIZE_CAPS,
    KeyValidationError,
    asset_key,
    cluster_of,
    customer_key,
    customer_kind_for,
    derivative_keys,
    extension_for,
    is_asset_key,
    is_customer_key,
    new_upload_id,
)


class VocabularyTests(unittest.TestCase):
    def test_the_vocabularies_are_the_ones_in_the_spec(self):
        self.assertEqual(PROGRAMMES, ("afs", "presales", "dealer"))
        self.assertEqual(ASSET_KINDS, ("photos", "videos", "tips", "docs"))
        self.assertEqual(CUSTOMER_KINDS, ("images", "videos", "docs"))
        self.assertEqual(SIZE_CAPS, {"images": 10 * 1024 * 1024, "videos": 100 * 1024 * 1024, "docs": 10 * 1024 * 1024})

    def test_extension_comes_from_the_mime_type(self):
        self.assertEqual(extension_for("image/jpeg"), "jpg")
        self.assertEqual(extension_for("image/png"), "png")
        self.assertEqual(extension_for("image/webp"), "webp")
        self.assertEqual(extension_for("video/mp4"), "mp4")
        self.assertEqual(extension_for("application/pdf"), "pdf")
        with self.assertRaises(KeyValidationError):
            extension_for("image/gif")

    def test_customer_kind_follows_the_mime_type(self):
        self.assertEqual(customer_kind_for("image/png"), "images")
        self.assertEqual(customer_kind_for("video/mp4"), "videos")
        self.assertEqual(customer_kind_for("application/pdf"), "docs")


class AssetKeyTests(unittest.TestCase):
    def test_a_well_formed_asset_key(self):
        self.assertEqual(
            asset_key("afs", "battery", "photos", "soc-button", "image/jpeg"),
            "assets/afs/battery/photos/soc-button.jpg",
        )

    def test_every_segment_is_validated(self):
        with self.assertRaises(KeyValidationError):
            asset_key("marketing", "battery", "photos", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "Battery", "photos", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "battery", "pictures", "x", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "battery", "photos", "SOC Button", "image/jpeg")
        with self.assertRaises(KeyValidationError):
            asset_key("afs", "../battery", "photos", "x", "image/jpeg")

    def test_derivatives_for_an_image_and_a_video(self):
        self.assertEqual(
            derivative_keys("assets/afs/battery/photos/soc-button.jpg"),
            {"w900": "assets/afs/battery/photos/soc-button.w900.webp"},
        )
        self.assertEqual(
            derivative_keys("assets/afs/battery/videos/key-turn.mp4"),
            {"poster": "assets/afs/battery/videos/key-turn.poster.jpg"},
        )
        self.assertEqual(derivative_keys("assets/afs/battery/docs/manual.pdf"), {})


class CustomerKeyTests(unittest.TestCase):
    def test_a_well_formed_customer_key(self):
        key = customer_key("clu_8f3a12", "conv_01J9K3", "images", "upl_01J9K4ab", "image/jpeg")
        self.assertEqual(key, "customers/clu_8f3a12/conv_01J9K3/images/upl_01J9K4ab.jpg")
        self.assertTrue(is_customer_key(key))
        self.assertFalse(is_asset_key(key))
        self.assertEqual(cluster_of(key), "clu_8f3a12")

    def test_a_phone_number_is_refused_as_a_cluster_id(self):
        for bad in ("+919876543210", "919876543210", "9876543210"):
            with self.assertRaises(KeyValidationError):
                customer_key(bad, "conv_1", "images", "upl_1", "image/jpeg")

    def test_kind_must_match_the_mime_type(self):
        with self.assertRaises(KeyValidationError):
            customer_key("clu_1", "conv_1", "videos", "upl_1", "image/jpeg")

    def test_upload_ids_sort_by_time(self):
        a = new_upload_id()
        b = new_upload_id()
        self.assertTrue(a.startswith("upl_") and b.startswith("upl_"))
        self.assertLessEqual(a[:14], b[:14])
        self.assertRegex(a, r"^upl_[0-9a-z]{10}[0-9a-z]{8}$")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python3 -m unittest tests.test_storage_keys -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'emotorad_ai.storage'`

- [ ] **Step 3: Write the module**

`src/emotorad_ai/storage/__init__.py`:

```python
"""Object storage for media: keys (pure), the S3 client, and the presign→attach
registry. Split so the key rules can be tested without AWS and boto3 is imported
in exactly one file."""
```

`src/emotorad_ai/storage/keys.py`:

```python
"""S3 key derivation. Every key the platform writes is produced here, from
closed vocabularies, and validated. The client never supplies a key.

Two trees:

    assets/<programme>/<category>/<kind>/<slug>.<ext>      authored by us
    customers/<cluster_id>/<conversation_id>/<kind>/<upload_id>.<ext>   evidence

Customer keys use the identity-graph cluster id, never the phone number: a phone
in a key is PII in every access log line. `programme` and both `kind` lists are
closed on purpose — adding one is a code change with a test, the same discipline
as the tool allowlists.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Dict

PROGRAMMES = ("afs", "presales", "dealer")
ASSET_KINDS = ("photos", "videos", "tips", "docs")
CUSTOMER_KINDS = ("images", "videos", "docs")

MIME_TYPES: Dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
}

SIZE_CAPS: Dict[str, int] = {
    "images": 10 * 1024 * 1024,
    "videos": 100 * 1024 * 1024,
    "docs": 10 * 1024 * 1024,
}

_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGITS = re.compile(r"^\+?\d{10,15}$")

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


class KeyValidationError(ValueError):
    """A segment is outside its vocabulary or shape. Raised before any AWS call."""


def extension_for(mime: str) -> str:
    try:
        return MIME_TYPES[mime]
    except KeyError:
        raise KeyValidationError("unsupported content type %r; allowed: %s" % (mime, ", ".join(MIME_TYPES))) from None


def customer_kind_for(mime: str) -> str:
    if mime.startswith("image/"):
        return "images"
    if mime.startswith("video/"):
        return "videos"
    return "docs"


def _segment(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.match(value):
        raise KeyValidationError("%s must match [a-z0-9][a-z0-9-]* (got %r)" % (name, value))
    return value


def _identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise KeyValidationError("%s must be an identifier (got %r)" % (name, value))
    if _DIGITS.match(value):
        # Looks like a phone number. The cluster id is the right identifier;
        # this check makes sure nothing upstream slipped the phone through.
        raise KeyValidationError("%s must not be a phone number" % name)
    return value


def asset_key(programme: str, category: str, kind: str, slug: str, mime: str) -> str:
    if programme not in PROGRAMMES:
        raise KeyValidationError("programme must be one of %s (got %r)" % (", ".join(PROGRAMMES), programme))
    if kind not in ASSET_KINDS:
        raise KeyValidationError("asset kind must be one of %s (got %r)" % (", ".join(ASSET_KINDS), kind))
    return "assets/%s/%s/%s/%s.%s" % (
        programme, _segment("category", category), kind, _segment("slug", slug), extension_for(mime)
    )


def derivative_keys(key: str) -> Dict[str, str]:
    """Derivatives are made once at upload time (no CDN resizes on request).
    Images get a 900px WebP; videos get a poster frame; documents get nothing."""
    stem, _, ext = key.rpartition(".")
    if ext in ("jpg", "png", "webp"):
        return {"w900": "%s.w900.webp" % stem}
    if ext == "mp4":
        return {"poster": "%s.poster.jpg" % stem}
    return {}


def customer_key(cluster_id: str, conversation_id: str, kind: str, upload_id: str, mime: str) -> str:
    if kind not in CUSTOMER_KINDS:
        raise KeyValidationError("customer kind must be one of %s (got %r)" % (", ".join(CUSTOMER_KINDS), kind))
    if customer_kind_for(mime) != kind:
        raise KeyValidationError("kind %r does not match content type %r" % (kind, mime))
    return "customers/%s/%s/%s/%s.%s" % (
        _identifier("cluster_id", cluster_id),
        _identifier("conversation_id", conversation_id),
        kind,
        _identifier("upload_id", upload_id),
        extension_for(mime),
    )


def new_upload_id() -> str:
    """Time-sortable: ten base-36 digits of milliseconds, then eight random."""
    ms = int(time.time() * 1000)
    digits = ""
    for _ in range(10):
        ms, rem = divmod(ms, 36)
        digits = _ALPHABET[rem] + digits
    random_part = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return "upl_%s%s" % (digits, random_part)


def is_customer_key(key: str) -> bool:
    return key.startswith("customers/")


def is_asset_key(key: str) -> bool:
    return key.startswith("assets/")


def cluster_of(key: str) -> str:
    if not is_customer_key(key):
        raise KeyValidationError("not a customer key: %r" % key)
    return key.split("/", 2)[1]
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python3 -m unittest tests.test_storage_keys -v` → 10 tests OK. Full suite green plus 10. `pyflakes` clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/storage tests/test_storage_keys.py
git commit -m "feat(storage): S3 key derivation from closed vocabularies

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `storage/s3.py`

**Files:**
- Create: `src/emotorad_ai/storage/s3.py`
- Modify: `requirements.txt` (add `Pillow>=10`)
- Test: `tests/test_storage_s3.py`

**Interfaces:**
- Produces: `BUCKET_ENV = "EMOTORAD_AI_MEDIA_BUCKET"`, `PUT_EXPIRY = 300`, `GET_EXPIRY = 900`, `S3Store(bucket, client=None, region=None)` with `presign_put(key, mime, size) -> Dict[str, Any]` (`{"url", "headers": {"Content-Type", "Content-Length"}, "expires_in"}`), `presign_get(key) -> str`, `head(key) -> Optional[Dict[str, Any]]` (`{"size", "mime"}` or `None` when missing), `get_bytes(key) -> bytes`, `put_bytes(key, data, mime) -> None`, `StorageError(Exception)`; `store_from_env(environ=None, client=None) -> Optional[S3Store]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_storage_s3.py
"""The one boto3 seam. Stubber verifies the exact request parameters, so a
presign that forgot to pin the content type cannot pass."""

import unittest
from unittest import mock

import boto3
from botocore.stub import Stubber

from emotorad_ai.storage.s3 import BUCKET_ENV, GET_EXPIRY, PUT_EXPIRY, S3Store, StorageError, store_from_env

KEY = "customers/clu_1/conv_1/images/upl_1.jpg"


def client():
    return boto3.client("s3", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="y")


class PresignTests(unittest.TestCase):
    def test_put_pins_type_and_length_and_expiry(self):
        c = client()
        store = S3Store("bucket-x", client=c)
        with mock.patch.object(c, "generate_presigned_url", return_value="https://signed/put") as gen:
            out = store.presign_put(KEY, "image/jpeg", 1234)
        gen.assert_called_once_with(
            "put_object",
            Params={"Bucket": "bucket-x", "Key": KEY, "ContentType": "image/jpeg", "ContentLength": 1234},
            ExpiresIn=PUT_EXPIRY,
            HttpMethod="PUT",
        )
        self.assertEqual(out, {"url": "https://signed/put", "headers": {"Content-Type": "image/jpeg", "Content-Length": "1234"}, "expires_in": PUT_EXPIRY})

    def test_get_uses_the_read_expiry(self):
        c = client()
        store = S3Store("bucket-x", client=c)
        with mock.patch.object(c, "generate_presigned_url", return_value="https://signed/get") as gen:
            self.assertEqual(store.presign_get(KEY), "https://signed/get")
        gen.assert_called_once_with("get_object", Params={"Bucket": "bucket-x", "Key": KEY}, ExpiresIn=GET_EXPIRY)


class ObjectTests(unittest.TestCase):
    def test_head_returns_size_and_type(self):
        c = client()
        stub = Stubber(c)
        stub.add_response("head_object", {"ContentLength": 1234, "ContentType": "image/jpeg"}, {"Bucket": "b", "Key": KEY})
        stub.activate()
        self.assertEqual(S3Store("b", client=c).head(KEY), {"size": 1234, "mime": "image/jpeg"})

    def test_head_returns_none_for_a_missing_object(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404, expected_params={"Bucket": "b", "Key": KEY})
        stub.activate()
        self.assertIsNone(S3Store("b", client=c).head(KEY))

    def test_head_raises_on_any_other_failure(self):
        c = client()
        stub = Stubber(c)
        stub.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403, expected_params={"Bucket": "b", "Key": KEY})
        stub.activate()
        with self.assertRaises(StorageError):
            S3Store("b", client=c).head(KEY)

    def test_get_and_put_bytes(self):
        import io

        c = client()
        stub = Stubber(c)
        stub.add_response("get_object", {"Body": io.BytesIO(b"abc")}, {"Bucket": "b", "Key": KEY})
        stub.add_response("put_object", {}, {"Bucket": "b", "Key": KEY, "Body": b"abc", "ContentType": "image/jpeg"})
        stub.activate()
        store = S3Store("b", client=c)
        self.assertEqual(store.get_bytes(KEY), b"abc")
        store.put_bytes(KEY, b"abc", "image/jpeg")
        stub.assert_no_pending_responses()


class FromEnvTests(unittest.TestCase):
    def test_no_bucket_means_no_store(self):
        self.assertIsNone(store_from_env(environ={}))

    def test_bucket_and_region_from_env(self):
        store = store_from_env(environ={BUCKET_ENV: "emotorad-ai-stage-media", "AWS_REGION": "ap-south-1"}, client=client())
        self.assertEqual(store.bucket, "emotorad-ai-stage-media")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: No module named 'emotorad_ai.storage.s3'`.

- [ ] **Step 3: Write the module and add Pillow**

```python
# src/emotorad_ai/storage/s3.py
"""The S3 client, and the only file that imports boto3 for media.

Presigned URLs are the whole design: the browser talks to S3 directly for the
bytes, and this service only ever signs. A PUT signature pins the content type
and length, so a client cannot presign a 2MB JPEG and then upload a 90MB file
under it. Reads are signed for fifteen minutes — long enough to load a
transcript, short enough that a leaked link is not a lasting one.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

BUCKET_ENV = "EMOTORAD_AI_MEDIA_BUCKET"
PUT_EXPIRY = 300
GET_EXPIRY = 900


class StorageError(Exception):
    """S3 answered with something other than success or not-found."""


class S3Store:
    def __init__(self, bucket: str, client: Any = None, region: Optional[str] = None) -> None:
        self.bucket = bucket
        if client is not None:
            self._client = client
        else:
            import boto3  # imported lazily: tests inject a client

            self._client = boto3.client("s3", region_name=region or os.environ.get("AWS_REGION", "ap-south-1"))

    def presign_put(self, key: str, mime: str, size: int) -> Dict[str, Any]:
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": mime, "ContentLength": size},
            ExpiresIn=PUT_EXPIRY,
            HttpMethod="PUT",
        )
        return {
            "url": url,
            "headers": {"Content-Type": mime, "Content-Length": str(size)},
            "expires_in": PUT_EXPIRY,
        }

    def presign_get(self, key: str) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=GET_EXPIRY
        )

    def head(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError("head %r failed: %s" % (key, code or type(exc).__name__)) from None
        return {"size": int(response["ContentLength"]), "mime": response.get("ContentType") or ""}

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise StorageError("get %r failed: %s" % (key, type(exc).__name__)) from None

    def put_bytes(self, key: str, data: bytes, mime: str) -> None:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)
        except Exception as exc:
            raise StorageError("put %r failed: %s" % (key, type(exc).__name__)) from None


def store_from_env(environ: Optional[Mapping[str, str]] = None, client: Any = None) -> Optional[S3Store]:
    """None when no bucket is configured: uploads are then disabled and the
    playground keeps its local blobs. Never a silent default bucket."""
    env = environ if environ is not None else os.environ
    bucket = env.get(BUCKET_ENV, "").strip()
    if not bucket:
        return None
    return S3Store(bucket, client=client, region=env.get("AWS_REGION"))
```

Append to `requirements.txt`:

```text

# Image derivatives for guide media (scripts/upload_asset.py and the assets
# upload path): a 900px WebP made once at upload time, since S3 does not resize
# on request the way Cloudinary did. imageio already pulls Pillow in; pinning it
# here says the dependency is deliberate.
Pillow>=10
```

Run `.venv/bin/pip install -q -r requirements.txt`.

- [ ] **Step 4: Run tests** — `tests.test_storage_s3` 8 OK; full suite green. pyflakes clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/storage/s3.py requirements.txt tests/test_storage_s3.py
git commit -m "feat(storage): S3Store with presigned put/get, head, and bytes; Pillow pinned

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `storage/uploads.py` — presign → attach registry

**Files:**
- Create: `src/emotorad_ai/storage/uploads.py`
- Test: `tests/test_storage_uploads.py`

**Interfaces:**
- Consumes: `keys.*`, `S3Store.presign_put/head`.
- Produces: `UploadError(ValueError)` with `.status` (400/404/409/413/415), `Pending` dataclass (`upload_id, key, mime, size, kind, tree, expires_at`), `Claimed` dataclass (`upload_id, key, mime, size, kind`), `UploadRegistry(store, clock=time.time)` with `begin_customer(cluster_id, conversation_id, mime, size) -> (Pending, presign_dict)`, `begin_asset(programme, category, kind, slug, mime, size) -> (Pending, presign_dict)`, `claim(upload_id) -> Claimed`, `forget(upload_id)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_storage_uploads.py
"""Presign, then claim. A claim verifies the object S3 actually holds against
what was presigned, so a client cannot upload something other than what it
declared and have the runtime trust it."""

import unittest

from emotorad_ai.storage.uploads import Claimed, UploadError, UploadRegistry


class _Store:
    def __init__(self):
        self.objects = {}
        self.presigned = []

    def presign_put(self, key, mime, size):
        self.presigned.append((key, mime, size))
        return {"url": "https://signed/" + key, "headers": {"Content-Type": mime, "Content-Length": str(size)}, "expires_in": 300}

    def head(self, key):
        return self.objects.get(key)


class BeginTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.now = [1_000.0]
        self.reg = UploadRegistry(self.store, clock=lambda: self.now[0])

    def test_customer_presign_derives_the_key_and_pins_type_and_size(self):
        pending, presign = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)
        self.assertTrue(pending.key.startswith("customers/clu_1/conv_1/images/upl_"))
        self.assertTrue(pending.key.endswith(".jpg"))
        self.assertEqual(pending.kind, "images")
        self.assertEqual(self.store.presigned, [(pending.key, "image/jpeg", 1234)])
        self.assertEqual(presign["headers"]["Content-Type"], "image/jpeg")
        self.assertEqual(pending.expires_at, 1_300.0)

    def test_asset_presign_uses_the_assets_tree(self):
        pending, _ = self.reg.begin_asset("afs", "battery", "photos", "soc-button", "image/png", 10)
        self.assertEqual(pending.key, "assets/afs/battery/photos/soc-button.png")
        self.assertEqual(pending.tree, "assets")

    def test_unsupported_type_is_415(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/gif", 10)
        self.assertEqual(caught.exception.status, 415)

    def test_over_cap_is_413_and_zero_is_400(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 10 * 1024 * 1024 + 1)
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 0)
        self.assertEqual(caught.exception.status, 400)

    def test_bad_path_segments_are_400(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.begin_asset("afs", "Battery", "photos", "x", "image/png", 10)
        self.assertEqual(caught.exception.status, 400)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        self.now = [1_000.0]
        self.reg = UploadRegistry(self.store, clock=lambda: self.now[0])
        self.pending, _ = self.reg.begin_customer("clu_1", "conv_1", "image/jpeg", 1234)

    def test_a_matching_object_is_claimed_once(self):
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        claimed = self.reg.claim(self.pending.upload_id)
        self.assertEqual(claimed, Claimed(self.pending.upload_id, self.pending.key, "image/jpeg", 1234, "images"))
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 404, "an id is single-use")

    def test_a_missing_object_is_409(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 409)

    def test_a_mismatched_size_or_type_is_409_and_the_id_stays(self):
        self.store.objects[self.pending.key] = {"size": 99, "mime": "image/jpeg"}
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 409)
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/png"}
        with self.assertRaises(UploadError):
            self.reg.claim(self.pending.upload_id)

    def test_an_unknown_id_is_404(self):
        with self.assertRaises(UploadError) as caught:
            self.reg.claim("upl_nope")
        self.assertEqual(caught.exception.status, 404)

    def test_an_expired_presign_cannot_be_claimed(self):
        self.store.objects[self.pending.key] = {"size": 1234, "mime": "image/jpeg"}
        self.now[0] = 1_000.0 + 300 + 3600 + 1
        with self.assertRaises(UploadError) as caught:
            self.reg.claim(self.pending.upload_id)
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
# src/emotorad_ai/storage/uploads.py
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
```

- [ ] **Step 4: Run tests** — 10 OK; full suite green; pyflakes clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/storage/uploads.py tests/test_storage_uploads.py
git commit -m "feat(storage): presign-then-claim upload registry with type and size verification

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `video.py` and `attachments.py` — shared content blocks

**Files:**
- Create: `src/emotorad_ai/video.py`, `src/emotorad_ai/attachments.py`
- Modify: `src/emotorad_ai/playground.py` (lines 638–792: delete the moved helpers and re-import them)
- Test: `tests/test_attachments.py`

**Interfaces:**
- Produces `video.py`: constants `VIDEO_FRAMES = 8`, `FRAME_MAX_EDGE`, `VIDEO_TYPES`, `WHISPER_MODEL`; functions `ffmpeg_exe()`, `extract_audio(data: bytes, suffix) -> Optional[bytes]`, `transcribe_audio(wav: bytes) -> Optional[Dict]`, `extract_frames(data: bytes, suffix, count=VIDEO_FRAMES) -> List[str]` (base64 JPEGs), `is_video(mime, name="") -> bool`. Bodies are the playground's, taking raw bytes instead of base64 strings.
- `playground.py` keeps working names: `_ffmpeg_exe = video.ffmpeg_exe`, `_extract_audio(data_b64, suffix)` = `video.extract_audio(base64.b64decode(data_b64), suffix)`, likewise `_extract_frames`, `_transcribe_audio = video.transcribe_audio`, `_is_video(attachment)` unchanged (calls `video.is_video`). `tests/test_video.py` passes unmodified.
- Produces `attachments.py`: `content_blocks(attachments: Sequence[Attachment], fetch: Optional[Callable[[str], bytes]] = None) -> List[Dict]` and `user_content(message: InboundMessage, fetch=None) -> Union[str, List[Dict]]`. A `data:` URL is decoded in place; an `s3://<key>` URL is fetched with `fetch(key)`; an `http(s)` image is passed as a URL source; anything else is dropped. Images → base64 image blocks; PDFs → document blocks; videos → transcript text block (if any) + frames block text + image blocks, or the "could not be read" text block; the message text comes last (`"(attachment)"` when empty). A fetch failure yields a text block `[An attachment could not be retrieved; do not describe it.]` rather than raising.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_attachments.py
"""What an attachment becomes in the model's history — in production, not only
in the playground. Text-only turns keep the plain-string shape they had."""

import base64
import unittest

from emotorad_ai.attachments import content_blocks, user_content
from emotorad_ai.contract import VERIFIED, Attachment, Identity, InboundMessage

PNG = base64.b64encode(b"\x89PNG fake").decode()


def message(text, attachments=()):
    return InboundMessage(
        "c1", "customer", Identity(strength=VERIFIED, phone="+919876543210"), "website_chat", text,
        attachments=list(attachments),
    )


class UserContentTests(unittest.TestCase):
    def test_text_only_is_still_a_plain_string(self):
        self.assertEqual(user_content(message("battery won't charge")), "battery won't charge")

    def test_a_data_url_image_becomes_a_base64_block_before_the_text(self):
        content = user_content(message("what is this light", [Attachment("image", "data:image/png;base64," + PNG, "image/png")]))
        self.assertEqual(content[0], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}})
        self.assertEqual(content[-1], {"type": "text", "text": "what is this light"})

    def test_an_s3_key_is_fetched_server_side_and_sent_as_base64(self):
        fetched = []

        def fetch(key):
            fetched.append(key)
            return b"\x89PNG fake"

        content = user_content(message("", [Attachment("image", "s3://customers/clu_1/conv_1/images/upl_1.png", "image/png")]), fetch=fetch)
        self.assertEqual(fetched, ["customers/clu_1/conv_1/images/upl_1.png"])
        self.assertEqual(content[0]["source"], {"type": "base64", "media_type": "image/png", "data": PNG})
        self.assertEqual(content[-1], {"type": "text", "text": "(attachment)"})

    def test_a_pdf_becomes_a_document_block(self):
        content = content_blocks([Attachment("document", "data:application/pdf;base64,QUJD", "application/pdf")])
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")

    def test_an_http_image_is_passed_by_url(self):
        content = content_blocks([Attachment("image", "https://cdn.test/a.jpg", "image/jpeg")])
        self.assertEqual(content, [{"type": "image", "source": {"type": "url", "url": "https://cdn.test/a.jpg"}}])

    def test_a_fetch_failure_becomes_a_warning_block_not_an_exception(self):
        def fetch(key):
            raise RuntimeError("boom")

        content = content_blocks([Attachment("image", "s3://customers/x/y/images/z.png", "image/png")], fetch=fetch)
        self.assertEqual(content, [{"type": "text", "text": "[An attachment could not be retrieved; do not describe it.]"}])

    def test_an_s3_key_with_no_fetcher_is_a_warning_block(self):
        content = content_blocks([Attachment("image", "s3://customers/x/y/images/z.png", "image/png")])
        self.assertEqual(content[0]["type"], "text")

    def test_an_unreadable_video_says_so(self):
        content = content_blocks([Attachment("video", "data:video/mp4;base64,AAAA", "video/mp4")])
        self.assertEqual(len(content), 1)
        self.assertIn("could not be read", content[0]["text"])

    def test_unknown_types_are_dropped(self):
        self.assertEqual(content_blocks([Attachment("document", "data:text/csv;base64,QQ==", "text/csv")]), [])


class PlaygroundCompatibilityTests(unittest.TestCase):
    def test_the_playground_still_exposes_its_helper_names(self):
        from emotorad_ai import playground, video

        self.assertIs(playground._transcribe_audio, video.transcribe_audio)
        self.assertEqual(playground.VIDEO_FRAMES, video.VIDEO_FRAMES)
        self.assertTrue(callable(playground._extract_frames))
        self.assertTrue(playground._is_video({"mime_type": "video/mp4"}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails** — `ModuleNotFoundError: emotorad_ai.attachments`.

- [ ] **Step 3: Create `video.py`** by moving lines 638–792 of `playground.py` (the constants `VIDEO_FRAMES`, `FRAME_MAX_EDGE`, `VIDEO_TYPES`, `WHISPER_MODEL`, `_whisper_cache`, and the functions `_ffmpeg_exe`, `_extract_audio`, `_transcribe_audio`, `_is_video`, `_extract_frames`) into `src/emotorad_ai/video.py` with these changes: drop the leading underscores (`ffmpeg_exe`, `extract_audio`, `transcribe_audio`, `extract_frames`), take `data: bytes` instead of `data_b64: str` (write `data` directly instead of `base64.b64decode(data_b64)`), and replace `_is_video(attachment)` with:

```python
def is_video(mime: str, name: str = "") -> bool:
    mime = (mime or "").lower()
    name = (name or "").lower()
    return mime.startswith("video/") or name.rsplit(".", 1)[-1] in VIDEO_TYPES
```

Module docstring: "Video evidence: frames and narration. Claude has no video block, so a clip is sampled into stills and its audio transcribed. Shared by the production agent loop and the playground; the playground did this first, and these are its functions moved, not rewritten."

In `playground.py`, delete the moved block and add after the existing imports:

```python
from emotorad_ai import video
from emotorad_ai.video import FRAME_MAX_EDGE, VIDEO_FRAMES, VIDEO_TYPES, WHISPER_MODEL  # noqa: F401  (kept for callers)

_ffmpeg_exe = video.ffmpeg_exe
_transcribe_audio = video.transcribe_audio


def _extract_audio(data_b64: str, suffix: str) -> Optional[bytes]:
    return video.extract_audio(base64.b64decode(data_b64), suffix)


def _extract_frames(data_b64: str, suffix: str, count: int = VIDEO_FRAMES) -> List[str]:
    return video.extract_frames(base64.b64decode(data_b64), suffix, count)


def _is_video(attachment: Dict[str, Any]) -> bool:
    return video.is_video(attachment.get("mime_type") or "", attachment.get("name") or "")
```

Run `tests.test_video` and `tests.test_playground_loop` before continuing; both must pass unchanged.

- [ ] **Step 4: Write `attachments.py`**

```python
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from . import video
from .contract import Attachment, InboundMessage

UNRETRIEVABLE = "[An attachment could not be retrieved; do not describe it.]"


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
            blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(payload).decode()}}
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
```

- [ ] **Step 5: Run tests** — `tests.test_attachments` 10 OK; `tests.test_video`, `tests.test_playground_loop` unchanged and green; full suite green; pyflakes clean.

- [ ] **Step 6: Commit**

```bash
git add src/emotorad_ai/video.py src/emotorad_ai/attachments.py src/emotorad_ai/playground.py tests/test_attachments.py
git commit -m "feat(attachments): shared content blocks for data, s3 and http attachments; video helpers move out of the playground

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Attachments reach the model in the production loop

**Files:**
- Modify: `src/emotorad_ai/agents/base.py` (`Agent.__init__`, line 83), `src/emotorad_ai/runtime.py` (`Runtime.__init__`, the `Agent(...)` construction)
- Test: `tests/test_agent_attachments.py`

**Interfaces:**
- Produces: `Agent(definition, registry, llm, log, settings, fetch=None)`; `Runtime(..., media_store=None)` storing `self.media_store` and passing `fetch=media_store.get_bytes if media_store else None` to every `Agent`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_attachments.py
"""An evidence photo sent over the API reaches the model as an image block, and
an S3 key is fetched through the store the runtime was given."""

import base64
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry

PNG = base64.b64encode(b"\x89PNG fake").decode()


class _Store:
    def __init__(self):
        self.fetched = []

    def get_bytes(self, key):
        self.fetched.append(key)
        return b"\x89PNG fake"


def make_runtime(script, store=None):
    registry = build_registry(today=date(2026, 8, 6))
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None), registry=registry, llm=llm,
        log=EventLog(path=None, to_stdout=False), resolver=IdentityResolver(registry), media_store=store,
    )
    return runtime, llm


def send(runtime, attachments, text="what is this light"):
    adapter = WebsiteChatAdapter(runtime.resolver)
    return runtime.handle(adapter.to_message({"conversation_id": "c1", "session_token": "sess-ananya", "text": text, "pill": "battery_issue", "attachments": attachments}))


class AttachmentTests(unittest.TestCase):
    def test_a_data_url_reaches_the_model_as_an_image_block(self):
        runtime, llm = make_runtime([say("That is the charge indicator.")])
        send(runtime, [{"kind": "image", "url": "data:image/png;base64," + PNG, "mime_type": "image/png"}])
        first_user_turn = llm.requests[0]["messages"][0]["content"]
        self.assertEqual(first_user_turn[0]["type"], "image")
        self.assertEqual(first_user_turn[-1], {"type": "text", "text": "what is this light"})

    def test_an_s3_key_is_fetched_through_the_runtime_store(self):
        store = _Store()
        runtime, llm = make_runtime([say("ok")], store=store)
        send(runtime, [{"kind": "image", "url": "s3://customers/clu_1/c1/images/upl_1.png", "mime_type": "image/png"}])
        self.assertEqual(store.fetched, ["customers/clu_1/c1/images/upl_1.png"])
        self.assertEqual(llm.requests[0]["messages"][0]["content"][0]["type"], "image")

    def test_text_only_history_is_unchanged(self):
        runtime, llm = make_runtime([say("ok")])
        send(runtime, [], text="battery won't charge")
        self.assertEqual(llm.requests[0]["messages"][0]["content"], "battery won't charge")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails** — `TypeError: Runtime.__init__() got an unexpected keyword argument 'media_store'`.

- [ ] **Step 3: Wire it**

`agents/base.py`: add `from ..attachments import user_content`; give `Agent.__init__` a trailing `fetch: Optional[Callable[[str], bytes]] = None` stored as `self.fetch` (add `Callable` to the typing import); change line 83 to `history.append({"role": "user", "content": user_content(message, self.fetch)})`.

`runtime.py`: read the file; add `media_store: Any = None` as the last `Runtime.__init__` parameter, `self.media_store = media_store`, and pass `fetch=media_store.get_bytes if media_store is not None else None` into every `Agent(...)` construction (there is one comprehension). Comment: "Evidence in S3 is fetched through the instance role and sent as base64; the model never sees a URL."

- [ ] **Step 4: Run tests** — 3 OK; full suite green; pyflakes clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/agents/base.py src/emotorad_ai/runtime.py tests/test_agent_attachments.py
git commit -m "feat(runtime): inbound attachments reach the model; S3 keys fetched through the media store

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: `media.resolve()` learns S3 ids

**Files:**
- Modify: `src/emotorad_ai/media.py` (`resolve`, `_delivery`; module docstring)
- Test: `tests/test_media_s3.py`

**Interfaces:**
- Produces: `media.resolve(item, store=None)`; an id containing `/` resolves through `store.presign_get("assets/" + id)` with `webp`/`poster` derivatives from `keys.derivative_keys`, returning `{"kind", "caption", "url", "poster", "fallback", "unresolved": False}`; with no store → `unresolved: True, reason: "EMOTORAD_AI_MEDIA_BUCKET is not set, so %r cannot be turned into a URL"`. Ids without `/` keep the Cloudinary path unchanged. `media.store_for_resolve()` returns `storage.s3.store_from_env()` cached per process (module-level, reset by tests via `media._store = None`); `resolve()` uses it when `store` is not passed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_media_s3.py
import unittest

from emotorad_ai import media
from emotorad_ai.media import resolve


class _Store:
    def presign_get(self, key):
        return "https://signed/" + key


class S3IdTests(unittest.TestCase):
    def test_an_id_with_a_slash_is_an_asset_key(self):
        out = resolve({"id": "afs/battery/photos/soc-button.jpg", "caption": "SOC button"}, store=_Store())
        self.assertEqual(out["url"], "https://signed/assets/afs/battery/photos/soc-button.w900.webp")
        self.assertEqual(out["fallback"], "https://signed/assets/afs/battery/photos/soc-button.jpg")
        self.assertIsNone(out["poster"])
        self.assertFalse(out["unresolved"])

    def test_a_video_gets_the_original_and_a_poster(self):
        out = resolve({"id": "afs/battery/videos/key-turn.mp4", "kind": "video", "caption": "c"}, store=_Store())
        self.assertEqual(out["url"], "https://signed/assets/afs/battery/videos/key-turn.mp4")
        self.assertEqual(out["poster"], "https://signed/assets/afs/battery/videos/key-turn.poster.jpg")

    def test_no_store_reports_the_gap_by_name(self):
        media._store = None
        with unittest.mock.patch.dict("os.environ", {"EMOTORAD_AI_MEDIA_BUCKET": ""}):
            out = resolve({"id": "afs/battery/photos/soc-button.jpg", "caption": "c"})
        self.assertTrue(out["unresolved"])
        self.assertIn("EMOTORAD_AI_MEDIA_BUCKET", out["reason"])

    def test_a_cloudinary_id_still_resolves_through_cloudinary(self):
        with unittest.mock.patch.dict("os.environ", {"EMOTORAD_CLOUDINARY_CLOUD": "cloudx"}):
            out = resolve({"id": "SOC_Button_non_doodle", "caption": "c"}, store=_Store())
        self.assertTrue(out["url"].startswith("https://res.cloudinary.com/cloudx/"))

    def test_an_absolute_url_is_untouched(self):
        out = resolve({"url": "https://cdn.test/a.png", "caption": "c"}, store=_Store())
        self.assertEqual(out["url"], "https://cdn.test/a.png")


if __name__ == "__main__":
    unittest.main()
```

Add `from unittest import mock` and use `mock.patch.dict` (the snippet writes `unittest.mock`; either is fine as long as it imports).

- [ ] **Step 2: Run to verify it fails** — `TypeError: resolve() got an unexpected keyword argument 'store'`.

- [ ] **Step 3: Change `media.py`**

Add near the top:

```python
from .storage import keys as storage_keys

_store: Any = None
_store_loaded = False


def store_for_resolve() -> Any:
    """The S3 store from the environment, built once per process. Tests reset
    `_store`/`_store_loaded` to force a rebuild."""
    global _store, _store_loaded
    if not _store_loaded:
        from .storage.s3 import store_from_env

        _store = store_from_env()
        _store_loaded = True
    return _store
```

In `resolve(item, store=None)`, after the absolute-URL branch and the "no id" branch, add before the Cloudinary delivery:

```python
    if "/" in public_id:
        # An S3 asset key (without the assets/ prefix). Ids with no slash are
        # Cloudinary public ids and keep working until migrated.
        s3 = store if store is not None else store_for_resolve()
        if s3 is None:
            resolved.update({"url": None, "unresolved": True, "reason": "%s is not set, so %r cannot be turned into a URL" % ("EMOTORAD_AI_MEDIA_BUCKET", public_id)})
            return resolved
        key = "assets/" + public_id.lstrip("/")
        derivatives = storage_keys.derivative_keys(key)
        original = s3.presign_get(key)
        if kind == "video":
            resolved.update({"url": original, "fallback": original, "poster": s3.presign_get(derivatives["poster"]) if "poster" in derivatives else None, "unresolved": False})
        else:
            resolved.update({"url": s3.presign_get(derivatives["w900"]) if "w900" in derivatives else original, "fallback": original, "unresolved": False})
        return resolved
```

Ensure `Any` is imported. Update the module docstring's "Records store an id, not a URL" paragraph to say: an id with a slash is an S3 key under `assets/` (extension included); an id without is a Cloudinary public id.

`test_no_store_reports_the_gap_by_name` needs `_store_loaded = False` too; set both in the test (`media._store = None; media._store_loaded = False`).

- [ ] **Step 4: Run tests** — 5 OK; `tests.test_media` unchanged and green; full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/media.py tests/test_media_s3.py
git commit -m "feat(media): resolve S3 asset ids with presigned reads and derivatives beside Cloudinary ids

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: API — `POST /uploads`, `GET /media/{key}`, attachments on `/message`

**Files:**
- Modify: `src/emotorad_ai/api.py`
- Test: `tests/test_api_uploads.py`

**Interfaces:**
- `MEDIA_STORE = store_from_env()` at import; `UPLOADS = UploadRegistry(MEDIA_STORE)` when configured; `Runtime(..., media_store=MEDIA_STORE)`.
- `POST /uploads` body `UploadIn {session_token: str = "", conversation_id: Optional[str], tree: "customers"|"assets", mime_type: str, size_bytes: int, path: Optional[{programme, category, kind, slug}]}` → 200 `{upload_id, key, url, headers, expires_in}`; 503 when no bucket; 401 for `assets` without playground basic auth; 400 when a customer upload has no `conversation_id` or the session resolves to no cluster; 400/413/415 from `UploadError`.
- `MessageIn.attachments: List[AttachmentIn] = []` where `AttachmentIn {upload_id: str}`; each is claimed and becomes `{"kind", "url": "s3://<key>", "mime_type"}` for the adapter; an `UploadError` maps to its status.
- `GET /media/{key:path}?session_token=` → 302 to a presigned GET; 503 without a bucket; 404 for keys outside `assets/`/`customers/`; 403 when a `customers/` key's cluster is not the session's cluster.
- `/health` gains `"media": "configured" | "not configured"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_uploads.py
"""The upload handshake over HTTP: presign, then attach, plus the read redirect.
The store is faked; no AWS is touched."""

import base64
import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


class _Store:
    bucket = "fake"

    def __init__(self):
        self.objects = {}
        self.fetched = []

    def presign_put(self, key, mime, size):
        return {"url": "https://signed/put/" + key, "headers": {"Content-Type": mime, "Content-Length": str(size)}, "expires_in": 300}

    def presign_get(self, key):
        return "https://signed/get/" + key

    def head(self, key):
        return self.objects.get(key)

    def get_bytes(self, key):
        self.fetched.append(key)
        return b"\x89PNG fake"


def fresh_api(store):
    with mock.patch.dict(os.environ, {"EMOTORAD_AI_MODE": "offline", "EMOTORAD_AI_MEDIA_BUCKET": "fake" if store else ""}):
        with mock.patch("emotorad_ai.storage.s3.store_from_env", return_value=store):
            import emotorad_ai.api as api

            api = importlib.reload(api)
    return api


AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:dev").decode()}


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = _Store()
        with mock.patch.dict(os.environ, {"EMOTORAD_AI_PLAYGROUND_USER": "dev", "EMOTORAD_AI_PLAYGROUND_PASSWORD": "dev"}):
            self.api = fresh_api(self.store)
        self.client = TestClient(self.api.app)

    def test_health_reports_media(self):
        self.assertEqual(self.client.get("/health").json()["media"], "configured")

    def test_customer_presign_then_attach(self):
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["key"].startswith("customers/"))
        self.assertNotIn("+91", body["key"])
        self.assertEqual(body["headers"]["Content-Type"], "image/png")
        self.store.objects[body["key"]] = {"size": 9, "mime": "image/png"}
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "what is this", "pill": "battery_issue", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.store.fetched, [body["key"]])

    def test_attaching_before_the_put_completes_is_409(self):
        body = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).json()
        r = self.client.post("/message", json={"conversation_id": "c1", "session_token": "sess-ananya", "text": "x", "attachments": [{"upload_id": body["upload_id"]}]})
        self.assertEqual(r.status_code, 409)

    def test_unsupported_type_and_over_cap(self):
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/gif", "size_bytes": 9})
        self.assertEqual(r.status_code, 415)
        r = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 11 * 1024 * 1024})
        self.assertEqual(r.status_code, 413)

    def test_asset_presign_needs_playground_auth(self):
        body = {"tree": "assets", "mime_type": "image/png", "size_bytes": 9, "path": {"programme": "afs", "category": "battery", "kind": "photos", "slug": "soc-button"}}
        self.assertEqual(self.client.post("/uploads", json=body).status_code, 401)
        r = self.client.post("/uploads", json=body, headers=AUTH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["key"], "assets/afs/battery/photos/soc-button.png")

    def test_media_redirects_to_a_presigned_get_for_the_owner_only(self):
        body = self.client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).json()
        r = self.client.get("/media/" + body["key"], params={"session_token": "sess-ananya"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "https://signed/get/" + body["key"])
        r = self.client.get("/media/" + body["key"], params={"session_token": "sess-rohit"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = self.client.get("/media/assets/afs/battery/photos/x.jpg", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/media/deploy/app.tar.gz", follow_redirects=False).status_code, 404)


class NoBucketTests(unittest.TestCase):
    def test_uploads_and_media_are_503_without_a_bucket(self):
        api = fresh_api(None)
        client = TestClient(api.app)
        self.assertEqual(client.get("/health").json()["media"], "not configured")
        self.assertEqual(client.post("/uploads", json={"session_token": "sess-ananya", "conversation_id": "c1", "tree": "customers", "mime_type": "image/png", "size_bytes": 9}).status_code, 503)
        self.assertEqual(client.get("/media/assets/x/y/photos/z.jpg", follow_redirects=False).status_code, 503)

    @classmethod
    def tearDownClass(cls):
        fresh_api(None)


if __name__ == "__main__":
    unittest.main()
```

Check `tests/test_identity_graph.py` or `tools/fixtures.py` for the second session token (`sess-rohit` is assumed; use whatever fixture token maps to Rohit's phone `+919812345678`).

- [ ] **Step 2: Run to verify it fails** — 404s on `/uploads` and KeyError on `media`.

- [ ] **Step 3: Implement in `api.py`**

Imports to add: `from typing import Any, Dict, List` (extend existing), `from .storage.keys import cluster_of, is_asset_key, is_customer_key`, `from .storage.s3 import store_from_env`, `from .storage.uploads import UploadError, UploadRegistry`. Ensure `Query` from fastapi and `Field` from pydantic if used.

After `settings = load_settings()`:

```python
# Media: None when EMOTORAD_AI_MEDIA_BUCKET is unset. Then /uploads and /media
# answer 503 with the reason, and the runtime sends no S3 evidence to the model.
MEDIA_STORE = store_from_env()
UPLOADS = UploadRegistry(MEDIA_STORE) if MEDIA_STORE is not None else None
```

Pass `media_store=MEDIA_STORE` to `Runtime(...)`. `/health` returns `{"status": "ok", "mode": MODE, "secrets": SECRETS_STATE, "media": "configured" if MEDIA_STORE is not None else "not configured"}`.

Models:

```python
class AttachmentIn(BaseModel):
    upload_id: str


class MessageIn(BaseModel):
    conversation_id: Optional[str] = None
    session_token: str = "sess-ananya"
    text: str
    pill: Optional[str] = None
    attachments: List[AttachmentIn] = []


class AssetPath(BaseModel):
    programme: str
    category: str
    kind: str
    slug: str


class UploadIn(BaseModel):
    session_token: str = ""
    conversation_id: Optional[str] = None
    tree: str
    mime_type: str
    size_bytes: int
    path: Optional[AssetPath] = None
```

Helpers and routes:

```python
def _require_media() -> None:
    if MEDIA_STORE is None or UPLOADS is None:
        raise HTTPException(503, "Media storage is not configured on this deployment (EMOTORAD_AI_MEDIA_BUCKET).")


def _cluster_for_session(session_token: str) -> str:
    """The identity-graph cluster the session resolves to. Never the phone."""
    _, identity = resolver.resolve_website(None, session_token or None)
    if not identity.cluster_id:
        raise HTTPException(400, "session does not resolve to a customer")
    return identity.cluster_id


@app.post("/uploads")
def post_upload(body: UploadIn, request: Request) -> Dict[str, Any]:
    _require_media()
    try:
        if body.tree == "customers":
            if not body.conversation_id:
                raise HTTPException(400, "conversation_id is required for customer uploads")
            cluster_id = _cluster_for_session(body.session_token)
            pending, presign = UPLOADS.begin_customer(cluster_id, body.conversation_id, body.mime_type, body.size_bytes)
        elif body.tree == "assets":
            require_playground_auth(request)
            if body.path is None:
                raise HTTPException(400, "path is required for asset uploads")
            pending, presign = UPLOADS.begin_asset(body.path.programme, body.path.category, body.path.kind, body.path.slug, body.mime_type, body.size_bytes)
        else:
            raise HTTPException(400, "tree must be 'customers' or 'assets'")
    except UploadError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return {"upload_id": pending.upload_id, "key": pending.key, **presign}


@app.get("/media/{key:path}")
def get_media(key: str, session_token: str = "") -> RedirectResponse:
    _require_media()
    if is_customer_key(key):
        if cluster_of(key) != _cluster_for_session(session_token):
            raise HTTPException(403, "not your attachment")
    elif not is_asset_key(key):
        raise HTTPException(404, "no such media")
    # A fresh 15-minute link every time, so a transcript rendered later still loads.
    return RedirectResponse(MEDIA_STORE.presign_get(key), status_code=302)
```

In `post_message`, before `adapter.to_message`, claim attachments:

```python
    attachments: List[Dict[str, Any]] = []
    if body.attachments:
        _require_media()
        try:
            for item in body.attachments:
                claimed = UPLOADS.claim(item.upload_id)
                attachments.append({"kind": claimed.kind.rstrip("s") if claimed.kind != "images" else "image", "url": "s3://" + claimed.key, "mime_type": claimed.mime})
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from None
```

Map `kind`: `images → image`, `videos → video`, `docs → document` (write a small dict rather than the rstrip trick). Pass `"attachments": attachments` into the adapter event dict. `require_playground_auth` is a plain function taking `Request`; calling it directly is fine.

- [ ] **Step 4: Run tests** — `tests.test_api_uploads` 8 OK; `tests.test_api_health` still green (add `"media"` to its expected dict); full suite green; pyflakes clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/api.py tests/test_api_uploads.py tests/test_api_health.py
git commit -m "feat(api): presigned uploads, attachments on /message, and /media read redirects

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Playground blobs on S3 when configured

**Files:**
- Modify: `src/emotorad_ai/playground.py` (`_put_blob`, `_get_blob`, `_externalise_attachments`)
- Test: `tests/test_playground_blobs.py`

**Interfaces:**
- `_put_blob(data_b64, chat_id="", mime_type="")` returns the blob id; when `media.store_for_resolve()` is not None it also `put_bytes` the decoded bytes at `customers/playground/<chat_id or "unknown">/<kind>/<blob_id>.<ext>` and returns the same id; the attachment record gains `"s3_key"`. `_get_blob` reads local first, then `get_bytes(s3_key)` when the local file is absent, caching it locally. `_externalise_attachments(attachments, chat_id)`; find its one caller and pass the chat id.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_playground_blobs.py
"""Playground attachments survive a redeploy on staging: bytes go to S3 under a
playground-only prefix, and are read back when the local cache is gone."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from emotorad_ai import media, playground

PNG_B64 = base64.b64encode(b"\x89PNG fake").decode()


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = (data, mime)

    def get_bytes(self, key):
        return self.objects[key][0]


class BlobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.blob_dir = Path(self.tmp.name)
        self.store = _Store()

    def tearDown(self):
        self.tmp.cleanup()

    def test_without_a_store_blobs_stay_local(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=None):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
        self.assertNotIn("s3_key", records[0])
        self.assertTrue((self.blob_dir / (records[0]["blob_id"] + ".b64")).exists())

    def test_with_a_store_bytes_go_to_s3_under_the_playground_prefix(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=self.store):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
        key = records[0]["s3_key"]
        self.assertEqual(key, "customers/playground/chat-1/images/%s.png" % records[0]["blob_id"])
        self.assertEqual(self.store.objects[key], (b"\x89PNG fake", "image/png"))

    def test_a_missing_local_cache_is_refilled_from_s3(self):
        with mock.patch.object(playground, "BLOB_DIR", self.blob_dir), mock.patch.object(media, "store_for_resolve", return_value=self.store):
            records = playground._externalise_attachments([{"name": "a.png", "kind": "image", "mime_type": "image/png", "data": PNG_B64}], "chat-1")
            (self.blob_dir / (records[0]["blob_id"] + ".b64")).unlink()
            self.assertEqual(playground._get_blob(records[0]), PNG_B64)
            self.assertTrue((self.blob_dir / (records[0]["blob_id"] + ".b64")).exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails** — `TypeError: _externalise_attachments() takes 1 positional argument`.

- [ ] **Step 3: Implement**, reading `playground.py` lines 340–420 first. `_put_blob` unchanged. In `_externalise_attachments(attachments, chat_id)`, after `record["blob_id"] = _put_blob(data)`:

```python
            store = media.store_for_resolve()
            if store is not None:
                # Staging: the container is rebuilt on every deploy, so a blob on
                # disk is gone by next week. The bytes go to S3 under a
                # playground-only prefix; the local file is a cache.
                mime = attachment.get("mime_type") or "application/octet-stream"
                try:
                    ext = storage_keys.extension_for(mime)
                except storage_keys.KeyValidationError:
                    ext = "bin"
                key = "customers/playground/%s/%s/%s.%s" % (chat_id or "unknown", storage_keys.customer_kind_for(mime), record["blob_id"], ext)
                store.put_bytes(key, base64.b64decode(data), mime)
                record["s3_key"] = key
```

Import `from emotorad_ai import media` (already imports `load_catalogue` from it; import the module too) and `from emotorad_ai.storage import keys as storage_keys`. In `_get_blob`, when the local read fails and `attachment.get("s3_key")`: `store = media.store_for_resolve()`; if not None, `data = base64.b64encode(store.get_bytes(key)).decode()`, write the local cache file, return it; on any exception return `""`. There are two callers of `_externalise_attachments` (around lines 480 and 1728); pass the chat id at both (`chat["chat_id"]`, or the id of the chat being rebuilt at line 480).

- [ ] **Step 4: Run tests** — 3 OK; `tests.test_playground_loop` green; full suite green; pyflakes clean.

- [ ] **Step 5: Commit**

```bash
git add src/emotorad_ai/playground.py tests/test_playground_blobs.py
git commit -m "feat(playground): attachment bytes go to S3 under customers/playground when a bucket is configured

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Scripts — `upload_asset.py` and `migrate_cloudinary.py`

**Files:**
- Create: `scripts/upload_asset.py`, `scripts/migrate_cloudinary.py`, `src/emotorad_ai/storage/derivatives.py`
- Test: `tests/test_derivatives.py`, `tests/test_scripts_assets.py`

**Interfaces:**
- `derivatives.image_w900_webp(data: bytes) -> bytes` (Pillow; shrink to 900 wide, never enlarge, RGB, WebP quality 82); `derivatives.video_poster_jpg(data: bytes) -> Optional[bytes]` (first frame via `video.extract_frames(data, ".mp4", count=1)`, decoded from base64).
- `scripts/upload_asset.py FILE --programme afs --category battery --kind photos --slug soc-button [--bucket B]`: derives the key with `keys.asset_key`, reads the file, `put_bytes` original + derivatives, prints `id: <key without assets/>` and the derivative keys. Uses `S3Store(bucket)` directly (staff credentials via the CLI profile); `--bucket` defaults to `EMOTORAD_AI_MEDIA_BUCKET`. Exposes `upload_asset(store, path, programme, category, kind, slug) -> Dict[str, str]` for tests.
- `scripts/migrate_cloudinary.py [--apply] [--bucket B]`: for every media item in `knowledge/**/*.yaml` and `knowledge/_media/catalogue.yaml` whose `id` has no `/`, downloads the Cloudinary original (`media._delivery(kind, "", id)` with the cloud name), uploads via `upload_asset` under `afs/<topic>/<photos|videos>/<slugified id>`, and prints the id mapping; with `--apply` rewrites the `id:` lines in those YAML files in place. Exposes `plan(records) -> List[Tuple[old_id, new_id]]` and `rewrite_ids(text, mapping) -> str` for tests. Downloading uses `urllib.request`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_derivatives.py
import io
import unittest

from PIL import Image

from emotorad_ai.storage.derivatives import image_w900_webp


def png(width, height):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class ImageDerivativeTests(unittest.TestCase):
    def test_a_wide_image_is_shrunk_to_900(self):
        out = Image.open(io.BytesIO(image_w900_webp(png(1800, 600))))
        self.assertEqual(out.format, "WEBP")
        self.assertEqual(out.size, (900, 300))

    def test_a_small_image_is_never_enlarged(self):
        out = Image.open(io.BytesIO(image_w900_webp(png(480, 320))))
        self.assertEqual(out.size, (480, 320))


if __name__ == "__main__":
    unittest.main()
```

```python
# tests/test_scripts_assets.py
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / ("%s.py" % name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Store:
    def __init__(self):
        self.objects = {}

    def put_bytes(self, key, data, mime):
        self.objects[key] = mime


class UploadAssetTests(unittest.TestCase):
    def test_original_and_derivative_are_uploaded_and_the_id_is_printed(self):
        upload_asset = load("upload_asset")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "soc.png"
            Image.new("RGB", (1200, 800)).save(path)
            store = _Store()
            out = upload_asset.upload_asset(store, str(path), "afs", "battery", "photos", "soc-button")
        self.assertEqual(out["id"], "afs/battery/photos/soc-button.png")
        self.assertEqual(store.objects, {"assets/afs/battery/photos/soc-button.png": "image/png", "assets/afs/battery/photos/soc-button.w900.webp": "image/webp"})


class MigrateTests(unittest.TestCase):
    def test_plan_maps_cloudinary_ids_to_asset_ids(self):
        migrate = load("migrate_cloudinary")
        records = [{"topic": "battery", "id": "SOC_Button_non_doodle", "kind": "image"}, {"topic": "battery", "id": "Battery_Revival_Steps.mp4", "kind": "video"}, {"topic": "motor", "id": "afs/motor/photos/pas.jpg", "kind": "image"}]
        self.assertEqual(
            migrate.plan(records),
            [("SOC_Button_non_doodle", "afs/battery/photos/soc-button-non-doodle.jpg"), ("Battery_Revival_Steps.mp4", "afs/battery/videos/battery-revival-steps.mp4")],
        )

    def test_rewrite_ids_touches_only_id_lines(self):
        migrate = load("migrate_cloudinary")
        text = "media:\n  - id: SOC_Button_non_doodle\n    caption: SOC_Button_non_doodle\n"
        out = migrate.rewrite_ids(text, {"SOC_Button_non_doodle": "afs/battery/photos/soc-button-non-doodle.jpg"})
        self.assertIn("  - id: afs/battery/photos/soc-button-non-doodle.jpg\n", out)
        self.assertIn("caption: SOC_Button_non_doodle", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail** — module not found.

- [ ] **Step 3: Write `derivatives.py`**

```python
# src/emotorad_ai/storage/derivatives.py
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
```

`Image.thumbnail((900, 9000))` preserves aspect and only shrinks, so a 1800×600 image becomes 900×300.

- [ ] **Step 4: Write `scripts/upload_asset.py`**

```python
#!/usr/bin/env python3
"""Upload one guide photo, clip or document to the media bucket, with its
derivatives, and print the `id:` line to paste into a knowledge record.

    .venv/bin/python3 scripts/upload_asset.py soc.png \
        --programme afs --category battery --kind photos --slug soc-button

Runs with your own AWS credentials (the emotorad-staging profile), not the API:
staff loading is a terminal task, and the bucket policy is the audit trail.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))

from emotorad_ai.storage import keys  # noqa: E402
from emotorad_ai.storage.derivatives import image_w900_webp, video_poster_jpg  # noqa: E402
from emotorad_ai.storage.s3 import BUCKET_ENV, S3Store  # noqa: E402


def upload_asset(store: Any, path: str, programme: str, category: str, kind: str, slug: str) -> Dict[str, str]:
    mime = mimetypes.guess_type(path)[0] or ""
    key = keys.asset_key(programme, category, kind, slug, mime)
    with open(path, "rb") as handle:
        data = handle.read()
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file")
    parser.add_argument("--programme", required=True, choices=keys.PROGRAMMES)
    parser.add_argument("--category", required=True)
    parser.add_argument("--kind", required=True, choices=keys.ASSET_KINDS)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--bucket", default=os.environ.get(BUCKET_ENV, ""))
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("--bucket or %s is required" % BUCKET_ENV)
    store = S3Store(args.bucket)
    written = upload_asset(store, args.file, args.programme, args.category, args.kind, args.slug)
    print("id: %s" % written["id"])
    for name, key in written.items():
        if name != "id":
            print("  %s -> s3://%s/%s" % (name, args.bucket, key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Write `scripts/migrate_cloudinary.py`**

```python
#!/usr/bin/env python3
"""Move guide media off Cloudinary into the media bucket, once.

    EMOTORAD_CLOUDINARY_CLOUD=... .venv/bin/python3 scripts/migrate_cloudinary.py          # plan only
    EMOTORAD_CLOUDINARY_CLOUD=... .venv/bin/python3 scripts/migrate_cloudinary.py --apply  # upload + rewrite ids

Every media `id` without a slash is a Cloudinary public id. Each becomes
`afs/<topic>/<photos|videos>/<slug>.<ext>` under assets/, and the YAML `id:`
lines are rewritten in place so nothing else changes. Review the diff, commit.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))

import yaml  # noqa: E402

from emotorad_ai import media  # noqa: E402
from emotorad_ai.storage.s3 import BUCKET_ENV, S3Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"


def slugify(public_id: str) -> str:
    stem = public_id.rsplit(".", 1)[0] if public_id.lower().endswith((".mp4", ".jpg", ".png", ".webp")) else public_id
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", stem.lower())).strip("-")


def plan(records: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in records:
        public_id = item["id"]
        if "/" in public_id or public_id in seen:
            continue
        seen.add(public_id)
        kind = item.get("kind", "image")
        folder, ext = ("videos", "mp4") if kind == "video" else ("photos", "jpg")
        out.append((public_id, "afs/%s/%s/%s.%s" % (item["topic"], folder, slugify(public_id), ext)))
    return out


def collect() -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    catalogue = KNOWLEDGE / media.CATALOGUE_PATH
    if catalogue.exists():
        for item in (yaml.safe_load(catalogue.read_text()) or {}).values():
            if item.get("id"):
                records.append({"topic": "battery", "id": item["id"], "kind": item.get("kind", "image")})
    for path in sorted(KNOWLEDGE.rglob("*.yaml")):
        if path.name == "catalogue.yaml":
            continue
        raw = yaml.safe_load(path.read_text()) or {}
        for item in raw.get("media") or []:
            if item.get("id"):
                records.append({"topic": raw.get("topic", "battery"), "id": item["id"], "kind": item.get("kind", "image")})
    return records


def rewrite_ids(text: str, mapping: Dict[str, str]) -> str:
    def replace(match: "re.Match[str]") -> str:
        return match.group(1) + mapping.get(match.group(2), match.group(2))

    return re.sub(r"^(\s*(?:-\s*)?id:\s*)(\S+)\s*$", replace, text, flags=re.MULTILINE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bucket", default=os.environ.get(BUCKET_ENV, ""))
    args = parser.parse_args(argv)
    mapping = dict(plan(collect()))
    for old, new in mapping.items():
        print("%s -> %s" % (old, new))
    if not args.apply:
        return 0
    if not args.bucket or not media.cloud_name():
        parser.error("--apply needs %s and EMOTORAD_CLOUDINARY_CLOUD" % BUCKET_ENV)
    from upload_asset import upload_asset  # sibling script

    store = S3Store(args.bucket)
    for old, new in mapping.items():
        kind = "video" if new.split("/")[2] == "videos" else "image"
        url = media._delivery(kind, "", old)
        data = urllib.request.urlopen(url, timeout=120).read()
        programme, topic, folder, filename = new.split("/")
        local = ROOT / ".playground" / "migrate" / filename
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        upload_asset(store, str(local), programme, topic, folder, filename.rsplit(".", 1)[0])
        print("uploaded %s" % new)
    for path in list(KNOWLEDGE.rglob("*.yaml")):
        text = path.read_text()
        new_text = rewrite_ids(text, mapping)
        if new_text != text:
            path.write_text(new_text)
            print("rewrote %s" % path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`from upload_asset import upload_asset` works because both scripts live in `scripts/` and Python puts the script's directory on `sys.path` when run directly.

- [ ] **Step 6: Run tests** — `tests.test_derivatives` 2 OK, `tests.test_scripts_assets` 3 OK; full suite green; pyflakes clean (the suite gate covers `scripts`).

- [ ] **Step 7: Commit**

```bash
git add src/emotorad_ai/storage/derivatives.py scripts/upload_asset.py scripts/migrate_cloudinary.py tests/test_derivatives.py tests/test_scripts_assets.py
git commit -m "feat(scripts): staff asset upload with derivatives, and the Cloudinary migration

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Infrastructure, runbook, workflow, docs

**Files:**
- Create: `infra/media.yaml`, `docs/runbooks/media.md`
- Modify: `.github/workflows/deploy-staging.yml`, `README.md`, `docs/Emotorad_AWS_Deployment_Plan.md` §2.3

- [ ] **Step 1: Template**

```yaml
# infra/media.yaml
# The media bucket: guide assets and customer evidence, private, versioned,
# with the two lifecycle rules and the CORS the browser needs to PUT directly.
#
#   aws cloudformation deploy --profile emotorad-staging --region ap-south-1 \
#     --stack-name emotorad-ai-stage-media --template-file infra/media.yaml \
#     --parameter-overrides Environment=stage InstanceRoleName=emotorad-ai-stage-ec2-role \
#       "AllowedOrigins=https://ai-release-stage.emotorad.com,http://localhost:8000" \
#     --capabilities CAPABILITY_NAMED_IAM
AWSTemplateFormatVersion: "2010-09-09"
Description: EMotorad AI platform - media bucket (assets + customer evidence) and instance-role access

Parameters:
  Environment:
    Type: String
    AllowedValues: [stage, prod]
  InstanceRoleName:
    Type: String
    Default: emotorad-ai-stage-ec2-role
  AllowedOrigins:
    Type: CommaDelimitedList
    Description: Origins allowed to PUT/GET directly against the bucket (the web chat's origins)
    Default: "https://ai-release-stage.emotorad.com,http://localhost:8000"

Resources:
  MediaBucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    Properties:
      BucketName: !Sub emotorad-ai-${Environment}-media
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      VersioningConfiguration:
        Status: Enabled
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256
      LifecycleConfiguration:
        Rules:
          - Id: customer-evidence-180d
            Status: Enabled
            Prefix: customers/
            ExpirationInDays: 180
            NoncurrentVersionExpiration:
              NoncurrentDays: 30
          - Id: abandoned-multipart
            Status: Enabled
            AbortIncompleteMultipartUpload:
              DaysAfterInitiation: 2
      CorsConfiguration:
        CorsRules:
          - AllowedMethods: [PUT, GET, HEAD]
            AllowedOrigins: !Ref AllowedOrigins
            AllowedHeaders: [Content-Type, Content-Length]
            ExposedHeaders: [ETag]
            MaxAge: 3600
      Tags:
        - Key: service
          Value: emotorad-ai
        - Key: environment
          Value: !Ref Environment

  MediaAccessPolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: !Sub emotorad-ai-${Environment}-media-access
      Roles:
        - !Ref InstanceRoleName
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Action: [s3:PutObject, s3:GetObject]
            Resource: !Sub arn:aws:s3:::emotorad-ai-${Environment}-media/*
          - Effect: Allow
            Action: s3:ListBucket
            Resource: !Sub arn:aws:s3:::emotorad-ai-${Environment}-media

Outputs:
  BucketName:
    Value: !Ref MediaBucket
```

(`HeadObject` is authorised by `s3:GetObject`; there is no separate action.) Validate with `aws cloudformation validate-template --profile emotorad-staging --region ap-south-1 --template-body file://infra/media.yaml --query 'Parameters[].ParameterKey' --output text` → `Environment InstanceRoleName AllowedOrigins`.

- [ ] **Step 2: Runbook `docs/runbooks/media.md`** with sections: 1 Create the bucket (the deploy command above; prod is a re-run with `Environment=prod` and the prod origins). 2 Configure the service (`EMOTORAD_AI_MEDIA_BUCKET` in the workflow; `/health` shows `"media":"configured"`). 3 Upload a guide asset (`scripts/upload_asset.py` example, then paste the printed `id:` into the record's `media:` list; commit). 4 The customer upload flow, as a curl walkthrough:

```bash
# 1. presign
curl -s -X POST http://127.0.0.1:8000/uploads -H 'Content-Type: application/json' \
  -d '{"session_token":"sess-ananya","conversation_id":"c1","tree":"customers","mime_type":"image/png","size_bytes":'"$(stat -f%z photo.png)"'}'
# 2. PUT the bytes to the returned url with the returned headers
curl -s -X PUT "<url>" -H 'Content-Type: image/png' --data-binary @photo.png
# 3. send the message with the upload id
curl -s -X POST http://127.0.0.1:8000/message -H 'Content-Type: application/json' \
  -d '{"conversation_id":"c1","session_token":"sess-ananya","text":"what is this light","attachments":[{"upload_id":"<upload_id>"}]}'
# 4. read it back
curl -s -o /dev/null -w '%{redirect_url}\n' "http://127.0.0.1:8000/media/<key>?session_token=sess-ananya"
```

5 Migrate off Cloudinary (`scripts/migrate_cloudinary.py`, then `--apply`, review the YAML diff, commit; afterwards remove `EMOTORAD_CLOUDINARY_CLOUD` from the workflow). 6 Delete a customer's evidence on request (`aws s3 rm --recursive s3://emotorad-ai-stage-media/customers/<cluster_id>/`). 7 Housekeeping: the old `playground-uploads/` object in the deploy bucket can be deleted.

- [ ] **Step 3: Workflow** — add `-e EMOTORAD_AI_MEDIA_BUCKET=emotorad-ai-stage-media` to the `docker run` line (one line, escaped quotes as the rest). Add a comment above the `env:` block noting the bucket name is not sensitive.

- [ ] **Step 4: README and deployment plan** — README module table: add rows for `storage/`, `attachments.py`, `video.py`; a short "Media" paragraph pointing at the runbook. Deployment plan §2.3: replace its three bullets with "Superseded 2026-09-21: the media bucket is `emotorad-ai-<env>-media`, created by `infra/media.yaml`; layout, upload flow and runbook in `docs/runbooks/media.md` and the media-storage spec."

- [ ] **Step 5: Checks** — full suite green; pyflakes clean; `validate-template` prints the three parameters; `.venv/bin/python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-staging.yml'))"` OK.

- [ ] **Step 6: Commit**

```bash
git add infra/media.yaml docs/runbooks/media.md .github/workflows/deploy-staging.yml README.md docs/Emotorad_AWS_Deployment_Plan.md
git commit -m "chore(media): bucket template, runbook, and the media bucket on the deploy

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 11: Apply on staging and prove it end to end (controller)

- [ ] Deploy the stack (runbook §1) with the `emotorad-staging` profile; confirm `emotorad-ai-stage-media` exists with versioning and the policy is attached to the instance role.
- [ ] Rebuild the local image; run it with `-v ~/.aws:/root/.aws:ro -e AWS_PROFILE=emotorad-staging -e EMOTORAD_AI_MEDIA_BUCKET=emotorad-ai-stage-media` plus the secret id and `EMOTORAD_AI_MODE=anthropic`; `/health` shows `"media":"configured"`.
- [ ] Walk the curl flow in runbook §4 with a real PNG: presign → PUT to S3 → `/message` with the upload id → the model's reply describes the picture → `/media/<key>` redirects to a working presigned URL. Confirm the object key under `customers/<cluster>/c1/images/` with `aws s3 ls`.
- [ ] Upload one guide asset with `scripts/upload_asset.py` (any small PNG), confirm the `.w900.webp` derivative exists, and `resolve({"id": "<printed id>"})` returns a signed URL that `curl -sI` answers 200.
- [ ] In the playground, attach an image in a chat; confirm it appears under `customers/playground/<chat_id>/` in the bucket.
- [ ] Hand over to the person for their own end-to-end test. Push and PR only after their yes.

---

## Self-review

**Spec coverage.** §2 bucket (versioning, SSE, lifecycle, CORS) → Task 10. §3 layout and rules → Task 1. §4 upload flow (presign, PUT direct, attach with HeadObject) → Tasks 3, 7. §5 reading back (base64 to the model, presigned to customers, `/media` redirect, Cloudinary compatibility) → Tasks 4, 5, 6, 7. §6 derivatives → Task 9. §7 modules → as listed. §8 infra → Task 10, applied in Task 11. §9 tests → each task. §10 out of scope: WhatsApp webhooks, CloudFront, virus scanning untouched.

**Type consistency.** `S3Store.head` returns `{"size", "mime"}` and `UploadRegistry.claim` reads exactly those keys. `user_content(message, fetch)` in Task 4 is what `Agent` calls in Task 5 with `self.fetch`. `media.resolve(item, store=None)` in Task 6; `store_for_resolve()` reused by the playground in Task 8. `keys.derivative_keys` used by Tasks 6 and 9. `UploadError.status` mapped in Task 7.

**Judgement calls.** Record ids carry the extension (spec §3.1 said without); recorded in Global Constraints. `assets/` reads via `/media` need no session (guide images are shown to anyone chatting and the link expires in 15 minutes). The playground's S3 prefix is `customers/playground/<chat_id>/...` so it falls under the 180-day rule. The multipart-abort lifecycle rule is a free safety net for interrupted large video PUTs.
