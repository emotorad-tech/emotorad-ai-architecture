"""Delivery URLs for the photos and clips authored into the knowledge base.

These are outbound aids — the picture of where the SOC button actually is, the
clip of the key turning — sent so a customer can find a thing rather than read a
paragraph describing it. They are authored per sub-issue in ``knowledge/*.yaml``
alongside the steps they illustrate, and reach the customer through
``OutboundMessage.attachments``. Nothing here is model-supplied: the model reads
captions and chooses a *record*, never a URL, so it cannot invent one.

**Records store an id, not a URL.** An id is an S3 asset key when its first
path segment is a programme from ``storage.keys.PROGRAMMES`` (``afs``,
``presales``, ``dealer``) — see ``is_asset_id()``. Such an id is looked up
under ``assets/`` (extension included), signed through the store from
``storage.s3``, with derivatives from ``storage.keys.derivative_keys``. Every
other id is a Cloudinary public id, e.g. ``SOC_Button_non_doodle`` or the
folder-qualified ``emotorad/kb/battery/soc-button`` — Cloudinary ids may
themselves contain slashes (Media Library folders), so a bare slash proves
nothing — rather than
``https://res.cloudinary.com/<cloud>/image/upload/f_auto,q_auto,w_900/...``.
The cloud name, the transformations and the format live here, in one place, so
changing any of them — or moving off Cloudinary entirely — is an edit to this
module rather than a hunt through every content file. Same reasoning as
``fixtures.warranty_term_months()``.

Absolute URLs still resolve untouched, so records written before this, and any
asset hosted elsewhere, keep working.

Why not Google Drive: a ``drive.google.com/file/d/<id>/view`` link serves an HTML
viewer page, not image bytes. There is no content type a chat client can render,
which is why it never worked and could not have been made to.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from .storage import keys as storage_keys

CLOUD_NAME_ENV = "EMOTORAD_CLOUDINARY_CLOUD"
BASE = "https://res.cloudinary.com"

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


# `f_auto` serves WebP/AVIF to clients that take them and JPEG to those that do
# not; `q_auto` picks a quality per image. Both matter more than they sound on
# Indian mobile data, where these are opened.
# `c_limit` shrinks to fit and never enlarges. Without it Cloudinary scales a
# small source *up* to the target width — the 480px SOC photo was being delivered
# as a blurrier, 67KB version of a sharp 36KB original.
IMAGE_TRANSFORM = "f_auto,q_auto,w_900,c_limit"
# Video is served untransformed, deliberately. Two things go wrong otherwise,
# both verified against the live asset:
#   * `f_auto` negotiates on the Accept header, so a browser asking for webm gets
#     `video/webm;codecs=vp9` back from a URL ending `.mp4`. The player is told
#     mp4, receives webm, and renders a dead 0:00 frame.
#   * any derivation is generated asynchronously, so the *first* request for it
#     returns 423 while Cloudinary builds it — and a video element that gets a
#     423 does not retry.
# The original h264/avc1 mp4 plays in every browser and on WhatsApp. It costs
# bytes (5MB here against 3.3MB of webm) and that is the right trade for a clip
# that has to play first time, on a phone, for someone already annoyed.
VIDEO_TRANSFORM = ""
# A still for the player to show before playback. `so_0` is the first frame.
# Delivered from the *video* resource, not the image one — the frame is extracted
# from the clip, so /image/upload/so_0/... has nothing to extract from and 404s.
POSTER_TRANSFORM = "so_0,f_jpg,q_auto,w_900,c_limit"

KINDS = ("image", "video")


def cloud_name() -> str:
    return os.environ.get(CLOUD_NAME_ENV, "")


def is_asset_id(public_id: str) -> bool:
    """S3 asset ids are `<programme>/<category>/<kind>/<slug>.<ext>`; the leading
    programme segment is the discriminator. Cloudinary ids may contain slashes
    (Media Library folders), so a bare slash proves nothing."""
    head, sep, _ = public_id.partition("/")
    return bool(sep) and head in storage_keys.PROGRAMMES


def _delivery(kind: str, transform: str, public_id: str) -> Optional[str]:
    cloud = cloud_name()
    if not cloud:
        return None
    resource = "video" if kind == "video" else "image"
    # An empty transform must not leave a doubled slash in the path.
    segments = [BASE, cloud, resource, "upload", transform, public_id.lstrip("/")]
    return "/".join(part for part in segments if part)


def resolve(item: Mapping[str, Any], store: Any = None) -> Dict[str, Any]:
    """One authored media item as something a chat client can render.

    Always returns a dict rather than raising or dropping the item. A photo that
    cannot be resolved is worth surfacing as a named gap — "this step has a guide
    image and it is not configured" — because silently sending no picture looks
    identical to a step that never had one, and the customer is left reading the
    paragraph the picture existed to replace.
    """
    kind = (item.get("kind") or "image").lower()
    if kind not in KINDS:
        kind = "image"
    caption = item.get("caption") or ""
    resolved: Dict[str, Any] = {"kind": kind, "caption": caption, "poster": None}

    url = item.get("url")
    if url:
        # Authored as an absolute URL: hosted somewhere else, or predates ids.
        resolved["url"] = url
        resolved["unresolved"] = False
        return resolved

    public_id = item.get("id")
    if not public_id:
        resolved.update({"url": None, "unresolved": True, "reason": "no id or url on this media item"})
        return resolved

    if is_asset_id(public_id):
        # An S3 asset key (without the assets/ prefix). Everything else, slash
        # or not, is a Cloudinary public id and keeps working unchanged.
        s3 = store if store is not None else store_for_resolve()
        if s3 is None:
            resolved.update(
                {
                    "url": None,
                    "unresolved": True,
                    "reason": "%s is not set, so %r cannot be turned into a URL"
                    % ("EMOTORAD_AI_MEDIA_BUCKET", public_id),
                }
            )
            return resolved
        key = "assets/" + public_id.lstrip("/")
        derivatives = storage_keys.derivative_keys(key)
        original = s3.presign_get(key)
        if kind == "video":
            resolved.update(
                {
                    "url": original,
                    "fallback": original,
                    "poster": s3.presign_get(derivatives["poster"]) if "poster" in derivatives else None,
                    "unresolved": False,
                }
            )
        else:
            resolved.update(
                {
                    "url": s3.presign_get(derivatives["w900"]) if "w900" in derivatives else original,
                    "fallback": original,
                    "unresolved": False,
                }
            )
        return resolved

    delivery = _delivery(kind, VIDEO_TRANSFORM if kind == "video" else IMAGE_TRANSFORM, public_id)
    if delivery is None:
        resolved.update(
            {
                "url": None,
                "unresolved": True,
                "reason": "%s is not set, so %r cannot be turned into a URL" % (CLOUD_NAME_ENV, public_id),
            }
        )
        return resolved

    resolved["url"] = delivery
    resolved["unresolved"] = False
    if kind == "video":
        resolved["poster"] = _delivery("video", POSTER_TRANSFORM, "%s.jpg" % public_id.rsplit(".", 1)[0])
    return resolved


# --- the guide-media catalogue ----------------------------------------------
# The fixed set of pictures the bot may send while the battery flow still lives
# in the prompt rather than in knowledge records. The model picks a key from this
# list; it never names a file or a URL, so there is nothing for it to invent or
# mistype. Interim by design — see knowledge/guide_media.yaml.

CATALOGUE_PATH = "_media/catalogue.yaml"


class CatalogueError(Exception):
    """A malformed catalogue. Raised at load, never at send time."""


def load_catalogue(directory: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """key -> media item, validated the same way knowledge records are."""
    import pathlib

    import yaml

    root = pathlib.Path(directory) if directory else pathlib.Path(__file__).resolve().parents[2] / "knowledge"
    path = root / CATALOGUE_PATH
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return {}
    if not isinstance(raw, dict):
        raise CatalogueError("%s: expected a mapping of key -> media item" % path)
    catalogue: Dict[str, Dict[str, Any]] = {}
    for key, item in raw.items():
        where = "%s: %s" % (path.name, key)
        if not isinstance(item, Mapping):
            raise CatalogueError("%s: each entry must be a mapping" % where)
        if not (item.get("id") or item.get("url")):
            raise CatalogueError("%s: needs an id (a CDN public id) or a url" % where)
        if not item.get("caption"):
            # The caption is what the model reasons about when choosing, and what
            # the customer reads under the picture. Without it the entry is a
            # filename the model has no basis for picking.
            raise CatalogueError("%s: needs a caption" % where)
        kind = item.get("kind", "image")
        if kind not in KINDS:
            raise CatalogueError("%s: kind must be 'image' or 'video', not %r" % (where, kind))
        catalogue[str(key)] = dict(item)
    return catalogue
