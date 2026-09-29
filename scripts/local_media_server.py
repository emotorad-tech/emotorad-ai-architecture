"""Run the chat server with a folder on this machine standing in for the media bucket.

    python scripts/local_media_server.py --dir C:\\Users\\me\\e2e-bucket --port 8000

For an end-to-end test on a machine with no AWS access: a photo sent from the
chat page is stored under its customer key in the folder, recorded in the
`media` collection (bucket "local-folder"), and read back for the model,
exactly as the server does with S3. Normally started for you by
`python scripts/chat_local.py --local-bucket <dir>`, which sets the rest of the
environment.

Only photos: a video goes to S3 through a presigned PUT from the browser, and
a folder cannot sign one, so /uploads answers 503 and the page asks for a
photo instead. Listens on 127.0.0.1 only. Never for customers.
"""

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.storage import s3  # noqa: E402
from emotorad_ai.storage.s3 import StorageError  # noqa: E402
from emotorad_ai.storage.uploads import UploadError  # noqa: E402

HOST = "127.0.0.1"


class DiskStore:
    """The four S3Store methods the server uses, and erasure, on a folder.

    The content type is kept beside each object (`<name>.mime`), as S3 keeps it
    on the object, so `head` can answer what a claim checks.
    """

    bucket = "local-folder"

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are derived by the server, but a folder is a filesystem: refuse
        # anything that would land outside it.
        if not key or key.startswith(("/", "\\")) or ":" in key:
            raise StorageError("key %r is not a relative key" % key)
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError("key %r leaves the folder" % key)
        return path

    def put_bytes(self, key: str, data: bytes, mime: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.with_name(path.name + ".mime").write_text(mime, encoding="utf-8")

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError("get %r failed: NoSuchKey" % key)
        return path.read_bytes()

    def head(self, key: str) -> Optional[Dict[str, Any]]:
        path = self._path(key)
        if not path.is_file():
            return None
        mime_file = path.with_name(path.name + ".mime")
        mime = mime_file.read_text(encoding="utf-8") if mime_file.is_file() else "application/octet-stream"
        return {"size": path.stat().st_size, "mime": mime}

    def presign_put(self, key: str, mime: str, size: int) -> Dict[str, Any]:
        raise UploadError(503, "Video upload needs the real S3 bucket; this server keeps media in a local folder.")

    def presign_get(self, key: str) -> str:
        raise StorageError("a local folder cannot sign a link for %r" % key)

    def delete_every_version(self, key: str) -> int:
        path = self._path(key)
        if not path.is_file():
            return 0
        path.unlink()
        mime_file = path.with_name(path.name + ".mime")
        if mime_file.is_file():
            mime_file.unlink()
        return 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="The chat server, with a local folder as the media bucket.")
    parser.add_argument("--dir", required=True, help="the folder that stands in for the bucket")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    store = DiskStore(args.dir)
    # Before emotorad_ai.api is imported: it builds MEDIA_STORE from this at import.
    s3.store_from_env = lambda *a, **k: store
    print("media bucket: local folder %s" % store.root, flush=True)

    import uvicorn

    uvicorn.run("emotorad_ai.api:app", host=HOST, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
