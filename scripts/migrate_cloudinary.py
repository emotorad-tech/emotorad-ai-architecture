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
import io
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))

import yaml  # noqa: E402
from PIL import Image  # noqa: E402

from emotorad_ai import media  # noqa: E402
from emotorad_ai.storage.s3 import BUCKET_ENV, S3Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "knowledge"

# Cloudinary's delivery URL in main() carries an empty transform, so the bytes
# come back in the source's own format, not the .jpg plan() always guesses for
# images. Sniffing the true format after download is what stops a PNG being
# uploaded and served as image/jpeg.
_FORMAT_TO_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}


def sniff_ext(data: bytes) -> str:
    fmt = Image.open(io.BytesIO(data)).format
    try:
        return _FORMAT_TO_EXT[fmt]
    except KeyError:
        raise ValueError("unsupported image format %r" % fmt) from None


def with_sniffed_ext(new_id: str, ext: str) -> str:
    stem, _, _old_ext = new_id.rpartition(".")
    return "%s.%s" % (stem, ext)


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
        if kind == "image":
            # The transform is empty, so Cloudinary returns the source's native
            # format, not the .jpg every image is planned with. Sniff the real
            # bytes and correct the id (and the mapping used to rewrite the
            # YAML below) before anything is written or uploaded.
            ext = sniff_ext(data)
            if not new.endswith("." + ext):
                new = with_sniffed_ext(new, ext)
                mapping[old] = new
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
