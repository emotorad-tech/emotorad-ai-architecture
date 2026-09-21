"""Container entrypoint: load startup config, then run both processes.

Order matters. `load_into_environ` fills this process's environment from the
Secrets Manager secret named by EMOTORAD_AI_SECRET_ID (a no-op when unset).
Streamlit is started as a child and uvicorn replaces this process via exec; both
inherit the environment, so no value is ever written to a file or a command line.

The Streamlit server binds to localhost only: api.py reverse-proxies /playground
to it, so it never needs a port opened in the security group.

`src_dir` finds the `src/` package directory whether this file is running from
a checkout (`docker/start.py` next to `src/`) or from the image (the Dockerfile
copies it to `/app/start.py`, beside `/app/src`).
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional


def src_dir(here: str) -> Optional[str]:
    for candidate in (os.path.join(here, "src"), os.path.join(here, os.pardir, "src")):
        # In the image start.py sits beside src/ (/app/start.py, /app/src); in a
        # checkout it is one level down (docker/start.py, src/). Try both.
        if os.path.isdir(candidate):
            return os.path.normpath(candidate)
    return None


_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = src_dir(_HERE)
if _SRC is not None:
    sys.path.insert(0, _SRC)

from emotorad_ai.config_store import ConfigStoreError, load_into_environ  # noqa: E402


def streamlit_command() -> List[str]:
    return [
        "streamlit", "run", "src/emotorad_ai/playground.py",
        "--server.port", "8501",
        "--server.address", "127.0.0.1",
        "--server.baseUrlPath", "playground",
        "--server.headless", "true",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false",
        "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "viewer",
    ]


def uvicorn_command() -> List[str]:
    return ["uvicorn", "emotorad_ai.api:app", "--host", "0.0.0.0", "--port", "8000"]


def main() -> int:
    try:
        exported = load_into_environ()
    except ConfigStoreError as exc:
        # Names only, never values — this line lands in CloudWatch.
        print("startup config: %s" % exc, file=sys.stderr, flush=True)
        return 1
    print("startup config: exported %s" % (", ".join(exported) or "nothing (no secret id set)"), flush=True)
    # Read by api.py's /health, before either child starts, so a container
    # that came up with a secret that exported nothing is visible as "empty"
    # rather than looking identical to "loaded".
    os.environ["EMOTORAD_AI_CONFIG_EXPORTED"] = str(len(exported))

    child = subprocess.Popen(streamlit_command())
    try:
        os.execvp("uvicorn", uvicorn_command())
    except OSError as exc:
        print("startup: could not exec uvicorn: %s" % type(exc).__name__, file=sys.stderr, flush=True)
        child.terminate()
        return 1
    return 0  # unreachable after a successful exec; kept for the tests' patched path


if __name__ == "__main__":
    raise SystemExit(main())
