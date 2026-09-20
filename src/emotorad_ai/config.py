"""Runtime settings. Everything is env-overridable so the same code runs against
mocks locally and against real systems in ECS without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # Claude on Bedrock keeps LLM traffic inside Emotorad's existing AWS boundary.
    # Bedrock model IDs carry the `anthropic.` prefix.
    model: str = os.environ.get("EMOTORAD_AI_MODEL", "anthropic.claude-opus-5")
    aws_region: str = os.environ.get("AWS_REGION", "ap-south-1")

    # Support chat is latency-sensitive (target: under 3s per turn), and battery
    # triage is a bounded problem — start low and raise per-route if evals say so.
    effort: str = os.environ.get("EMOTORAD_AI_EFFORT", "low")
    max_tokens: int = int(os.environ.get("EMOTORAD_AI_MAX_TOKENS", "16000"))

    # Hard ceiling on tool-calling round trips in a single turn.
    max_agent_iterations: int = int(os.environ.get("EMOTORAD_AI_MAX_ITERATIONS", "6"))

    log_path: str = os.environ.get("EMOTORAD_AI_LOG_PATH", "logs/conversations.jsonl")
    log_to_stdout: bool = os.environ.get("EMOTORAD_AI_LOG_STDOUT", "0") == "1"

    # Who approves a replacement order the bot has decided on. See
    # docs/superpowers/specs/2026-09-20-replacement-fulfilment-design.md.
    #   bot        - the bot approves sure and not-sure cases alike
    #   reasonable - the bot approves sure cases; not-sure waits for a human
    #   human      - everything waits for a human
    # The business-facing names for a panel later are "Bot in love with
    # customer", "Reasonable bot", "No brain, human approval only".
    approval_mode: str = field(default_factory=lambda: os.environ.get("EMOTORAD_AI_APPROVAL_MODE", "reasonable"))


APPROVAL_MODES = ("bot", "reasonable", "human")


def load_settings() -> Settings:
    settings = Settings()
    if settings.approval_mode not in APPROVAL_MODES:
        # A typo must not silently become "the bot approves everything".
        raise ValueError(
            "EMOTORAD_AI_APPROVAL_MODE must be one of %s, not %r"
            % (", ".join(APPROVAL_MODES), settings.approval_mode)
        )
    return settings
