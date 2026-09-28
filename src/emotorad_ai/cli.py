"""Run a conversation against the skeleton from the terminal.

    # offline: no AWS, no tokens — a fixed planner stands in for the model,
    # so the contract, guardrails, routing, tools and logging are all real
    python -m emotorad_ai.cli --offline

    # against Claude on Bedrock (needs AWS credentials for the account/region)
    python -m emotorad_ai.cli --session sess-ananya

    # as the signed-in Amiigo test rider (two bikes, see tools/fixtures.py)
    python -m emotorad_ai.cli --offline --channel amiigo --session sess-amiigo-test

    # Jev routing + OpenRouter models (needs OPENROUTER_API_KEY)
    python -m emotorad_ai.cli --mode openrouter --channel amiigo --session sess-amiigo-test

Offline mode is what you use to demo the safety hard-stop and the escalation
path, because neither of those calls the model at all.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from typing import Optional, Sequence

from .adapters import AmiigoAdapter, WebsiteChatAdapter
from .config import MODES, load_settings
from .contract import new_conversation_id
from .identity import IdentityResolver
from .observability import EventLog
from .runtime import Runtime
from .tools.mocks import build_registry
from .wiring import build_models

# Both carry a session token, which is all the CLI supplies.
ADAPTERS = {"website": WebsiteChatAdapter, "amiigo": AmiigoAdapter}


def resolve_mode(offline: bool, mode: Optional[str], environ=os.environ) -> str:
    """--offline, then --mode, then EMOTORAD_AI_MODE. With none of them the CLI
    talks to Bedrock, exactly as it did before modes existed."""
    if offline:
        return "offline"
    if mode:
        return mode
    return environ.get("EMOTORAD_AI_MODE") or "bedrock"


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Run an Emotorad battery-support conversation.")
    parser.add_argument("--session", default="sess-ananya", help="website or Amiigo session token (see tools/fixtures.py)")
    parser.add_argument("--channel", choices=sorted(ADAPTERS), default="website", help="which channel adapter the message arrives through")
    parser.add_argument("--pill", default=None, help="entry pill the visitor tapped, e.g. battery_issue")
    parser.add_argument("--offline", action="store_true", help="use the offline planner instead of Bedrock")
    parser.add_argument("--mode", choices=MODES, default=None, help="offline, bedrock or openrouter; overrides EMOTORAD_AI_MODE")
    parser.add_argument("--diagnostics", action="store_true", help="pretend battery telematics exist")
    parser.add_argument("message", nargs="*", help="one-shot message; omit for an interactive session")
    args = parser.parse_args(list(argv) or sys.argv[1:])

    settings = replace(load_settings(), mode=resolve_mode(args.offline, args.mode))
    registry = build_registry(diagnostics_available=args.diagnostics)
    log = EventLog(path=settings.log_path, to_stdout=False)
    models = build_models(settings)
    runtime = Runtime(
        settings=settings,
        registry=registry,
        llm=models.llm,
        narrow_llm=models.narrow_llm,
        jev=models.jev,
        log=log,
        resolver=IdentityResolver(registry),
    )
    adapter = ADAPTERS[args.channel](runtime.resolver)
    conversation_id = new_conversation_id()

    def send(text: str) -> None:
        message = adapter.to_message(
            {
                "conversation_id": conversation_id,
                "session_token": args.session,
                "text": text,
                "pill": args.pill,
            }
        )
        reply = runtime.handle(message)
        print("\nassistant: %s" % reply.text)
        if reply.escalated:
            print("[escalated to a human%s]" % (", ticket %s" % reply.ticket_id if reply.ticket_id else ""))
        print("[handled_by=%s tools=%s]" % (reply.handled_by, reply.metadata.get("tool_calls", [])))

    if args.message:
        send(" ".join(args.message))
        return 0

    print("Emotorad battery support (%s). Ctrl-C or an empty line to quit." % settings.mode)
    while True:
        try:
            text = input("\nyou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            break
        send(text)
    print("\nTranscript logged to %s" % settings.log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
