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
