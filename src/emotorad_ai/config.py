"""Runtime settings. Everything is env-overridable so the same code runs against
mocks locally and against real systems in ECS without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Which models answer. `offline` is a fixed planner, no network; `bedrock` is
# Claude in EMotorad's own AWS account; `openrouter` is Jev routing plus the
# OpenRouter reply models, and sends customer text outside AWS, so it needs
# sign-off before real customer traffic (spec §1.8).
MODES = ("offline", "bedrock", "openrouter")


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

    mode: str = os.environ.get("EMOTORAD_AI_MODE", "offline")

    # OpenRouter. The key is deliberately not here: Settings gets printed and
    # logged, a credential must not be. The transport reads it from the
    # environment itself.
    openrouter_base_url: str = os.environ.get("EMOTORAD_OPENROUTER_BASE_URL", "https://openrouter.ai/api")
    jev_model: str = os.environ.get("EMOTORAD_JEV_MODEL", "typesafe/jev-1.13")
    narrow_model: str = os.environ.get("EMOTORAD_NARROW_MODEL", "deepseek/deepseek-v4-flash-0731")
    fallback_model: str = os.environ.get("EMOTORAD_FALLBACK_MODEL", "anthropic/claude-haiku-4.5")
    # Jev sits in front of every turn, so it gets a tight budget: a slow answer
    # falls back to the full agent rather than holding the customer up.
    jev_timeout: float = float(os.environ.get("EMOTORAD_JEV_TIMEOUT", "2.0"))
    openrouter_timeout: float = float(os.environ.get("EMOTORAD_OPENROUTER_TIMEOUT", "30"))
    # Zero-data-retention providers only, unless someone deliberately turns it off.
    openrouter_zdr: bool = os.environ.get("EMOTORAD_OPENROUTER_ZDR", "1") == "1"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError("EMOTORAD_AI_MODE must be one of %s, not %r" % (", ".join(MODES), self.mode))


def load_settings() -> Settings:
    return Settings()
