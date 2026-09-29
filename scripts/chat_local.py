"""Run the /chat test page on this machine against the new chatbot.

    python scripts/chat_local.py                      # openrouter + mongodb, as staging runs
    python scripts/chat_local.py --store memory       # openrouter, nothing kept after a restart
    python scripts/chat_local.py --mode offline --store memory   # no key, no cost

Then open the address it prints. It starts the same server staging runs
(`emotorad_ai.api`), with the settings staging uses: Jev routing and the
OpenRouter models (EMOTORAD_AI_MODE=openrouter) and conversations in MongoDB
(EMOTORAD_STORE=mongodb). Photos attach inline from the page; videos need the
media bucket (EMOTORAD_AI_MEDIA_BUCKET) and are off without it.

Verification codes are shown on the page (EMOTORAD_AI_DEV_CODES=1) behind the
playground login, because no SMS is wired. That is a login as any phone for
anyone who can reach the page, so the server only ever listens on 127.0.0.1.

Secrets are read from the environment and never printed: each is reported as
set or missing, and the server does not start with one missing. Run by a
person: with --store mongodb it writes to the real Atlas cluster.

Not for customers. `openrouter` sends what is typed to OpenRouter, outside AWS
(see CLAUDE.md); use the fixture customers and nothing real. For the same
reason the business tools are always the fixtures here: EMOTORAD_OMS_API_KEY
is left out of the server's environment, so no lookup reaches the live OMS.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
# What the server binds to: this machine only.
HOST = "127.0.0.1"
# What the printed addresses use. The media bucket's CORS allows
# http://localhost:8000 for local testing (docs/runbooks/media.md section 1),
# and 127.0.0.1 is a different origin to a browser, so a page opened there
# has its video upload to S3 refused. Both addresses use it: a browser
# signed in on one origin is not signed in on the other.
PAGE_HOST = "localhost"
MODES = ("openrouter", "offline", "anthropic", "bedrock")
STORES = ("mongodb", "memory")
# The login in front of the code panel when none is set. Local only: the
# server this starts cannot be reached from another machine.
LOCAL_LOGIN = ("dev", "dev")

# What each choice cannot start without. Bedrock uses the AWS credentials
# chain, which is not one variable, so it is left to fail on start with its
# own message.
NEEDS = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "mongodb": ("EMOTORAD_MONGO_URI",),
}
# Never passed to the server. With it, a phone number typed on the page reaches
# the live purchase table (api._build_registry), and those details would go to
# OpenRouter with the rest of the conversation.
WITHHELD = ("EMOTORAD_OMS_API_KEY",)
# Reported but not required: each switches one feature on.
OPTIONAL = (
    ("EMOTORAD_AI_MEDIA_BUCKET", "photo and video storage in S3"),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse tracing"),
)


def _is_set(environ: Mapping[str, str], name: str) -> bool:
    return bool((environ.get(name) or "").strip())


def missing_secrets(environ: Mapping[str, str], mode: str, store: str) -> List[str]:
    return [name for name in NEEDS.get(mode, ()) + NEEDS.get(store, ()) if not _is_set(environ, name)]


def status_lines(environ: Mapping[str, str], mode: str, store: str) -> List[str]:
    """What is configured, by name only. A value never reaches this output."""
    lines = ["mode: %s   store: %s" % (mode, store)]
    for name in NEEDS.get(mode, ()) + NEEDS.get(store, ()):
        lines.append("%s: %s" % (name, "set" if _is_set(environ, name) else "MISSING"))
    if store == "mongodb":
        lines.append("MongoDB database: %s" % (environ.get("EMOTORAD_MONGO_DB") or "emotorad_ai"))
    for name, feature in OPTIONAL:
        lines.append("%s: %s (%s)" % (name, "set" if _is_set(environ, name) else "not set", feature))
    ignored = " (EMOTORAD_OMS_API_KEY is set and is ignored here)" if _is_set(environ, "EMOTORAD_OMS_API_KEY") else ""
    lines.append("business tools: fixtures, never the live OMS" + ignored)
    return lines


def server_env(environ: Mapping[str, str], mode: str, store: str) -> dict:
    """The server's environment: a copy of this one plus the test page's settings."""
    env = {name: value for name, value in environ.items() if name not in WITHHELD}
    env["EMOTORAD_AI_MODE"] = mode
    env["EMOTORAD_STORE"] = store
    env["EMOTORAD_AI_DEV_CODES"] = "1"
    if not (_is_set(env, "EMOTORAD_AI_PLAYGROUND_USER") and _is_set(env, "EMOTORAD_AI_PLAYGROUND_PASSWORD")):
        env["EMOTORAD_AI_PLAYGROUND_USER"], env["EMOTORAD_AI_PLAYGROUND_PASSWORD"] = LOCAL_LOGIN
    paths = [str(ROOT / "src"), str(ROOT)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def chat_url(mode: str, port: int) -> str:
    # Jev routes only in openrouter mode. Without it, pin the agent the prompts
    # were tuned on, as the page always did, rather than the keyword triage.
    query = "debug=1" if mode == "openrouter" else "agent=battery_support&debug=1"
    return "http://%s:%d/chat?%s" % (PAGE_HOST, port, query)


def sign_in_url(port: int) -> str:
    """Where to sign in once so the page can show the code. The page's fetch
    cannot ask for a login, but a browser signed in anywhere under
    /dev/verification/ sends it for every later request there."""
    return "http://%s:%d/dev/verification/sign-in" % (PAGE_HOST, port)


def e2e_url(port: int) -> str:
    """The end-to-end test console (web/e2e-console.html), behind the same login."""
    return "http://%s:%d/dev/e2e" % (PAGE_HOST, port)


def server_command(port: int, local_bucket: Optional[str] = None) -> List[str]:
    if local_bucket:
        # A folder in place of the media bucket, for a machine with no AWS
        # access: the same server, started by scripts/local_media_server.py.
        return [sys.executable, str(ROOT / "scripts" / "local_media_server.py"),
                "--dir", local_bucket, "--port", str(port)]
    return [sys.executable, "-m", "uvicorn", "emotorad_ai.api:app", "--host", HOST, "--port", str(port)]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the /chat test page against the new chatbot.")
    parser.add_argument("--mode", choices=MODES, default="openrouter")
    parser.add_argument("--store", choices=STORES, default="mongodb")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--local-bucket", metavar="DIR", default=None,
                        help="keep photos in this folder instead of the S3 bucket (no AWS needed; photos only)")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    for line in status_lines(os.environ, args.mode, args.store):
        print(line)
    missing = missing_secrets(os.environ, args.mode, args.store)
    if missing:
        print("\nNot starting: set %s in your environment first (never paste it here)." % ", ".join(missing))
        return 2
    env = server_env(os.environ, args.mode, args.store)
    login = (env["EMOTORAD_AI_PLAYGROUND_USER"], env["EMOTORAD_AI_PLAYGROUND_PASSWORD"])
    who = "%s / %s" % login if login == LOCAL_LOGIN else "your playground login"
    print("\n1. Open %s and sign in with %s." % (sign_in_url(args.port), who))
    print("   That lets the page show the verification code. You will see a line of JSON.")
    print("2. Then open %s" % chat_url(args.mode, args.port))
    print("3. The end-to-end test console: %s" % e2e_url(args.port))
    if args.local_bucket:
        print("Photos are kept in the folder %s, not in S3." % args.local_bucket)
    # Flushed, so the address is on screen before the server's own output
    # when this runs with its output piped (an IDE, a log file).
    print("Stop with Ctrl+C.\n", flush=True)
    try:
        return subprocess.call(server_command(args.port, args.local_bucket), env=env, cwd=str(ROOT))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
