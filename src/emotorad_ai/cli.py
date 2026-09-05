"""Run a conversation against the skeleton from the terminal.

    # offline: no AWS, no tokens — a fixed planner stands in for the model,
    # so the contract, guardrails, routing, tools and logging are all real
    python -m emotorad_ai.cli --offline

    # against Claude on Bedrock (needs AWS credentials for the account/region)
    python -m emotorad_ai.cli --session sess-ananya

Offline mode is what you use to demo the safety hard-stop and the escalation
path, because neither of those calls the model at all.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .adapters import WebsiteChatAdapter
from .bots import BotCatalogue
from .config import load_settings
from .contract import new_conversation_id
from .identity import IdentityResolver
from .llm import OfflinePlanner
from .observability import EventLog
from .runtime import Runtime
from .tools.mocks import build_registry


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Run an Emotorad battery-support conversation.")
    parser.add_argument("--session", default="sess-ananya", help="website session token (see tools/fixtures.py)")
    parser.add_argument("--pill", default=None, help="entry pill the visitor tapped, e.g. battery_issue")
    parser.add_argument("--offline", action="store_true", help="use the offline planner instead of Bedrock")
    parser.add_argument("--diagnostics", action="store_true", help="pretend battery telematics exist")
    parser.add_argument("message", nargs="*", help="one-shot message; omit for an interactive session")
    args = parser.parse_args(list(argv) or sys.argv[1:])

    settings = load_settings()
    catalogue = BotCatalogue.load()
    registry = build_registry(diagnostics_available=args.diagnostics, topics=catalogue.topics())
    log = EventLog(path=settings.log_path, to_stdout=False)
    runtime = Runtime(
        settings=settings,
        registry=registry,
        llm=OfflinePlanner() if args.offline else None,
        log=log,
        resolver=IdentityResolver(registry),
        catalogue=catalogue,
    )
    adapter = WebsiteChatAdapter(runtime.resolver)
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

    print("Emotorad battery support (%s). Ctrl-C or an empty line to quit." % ("offline" if args.offline else settings.model))
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
