"""Container entrypoint: load startup config, then run both processes.

Order matters. `load_into_environ` fills this process's environment from the
Secrets Manager secret named by EMOTORAD_AI_SECRET_ID (a no-op when unset).
Streamlit is started as a child and uvicorn replaces this process via exec; both
inherit the environment, so no value is ever written to a file or a command line.

The Streamlit server binds to localhost only: api.py reverse-proxies /playground
to it, so it never needs a port opened in the security group.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src"))

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
        print("startup config: %s" % exc, file=sys.stderr)
        return 1
    print("startup config: exported %s" % (", ".join(exported) or "nothing (no secret id set)"))

    subprocess.Popen(streamlit_command())
    os.execvp("uvicorn", uvicorn_command())
    return 0  # unreachable after a successful exec; kept for the tests' patched path


if __name__ == "__main__":
    raise SystemExit(main())
