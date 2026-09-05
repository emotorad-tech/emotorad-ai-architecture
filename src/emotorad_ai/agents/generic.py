"""A sub-agent from a spec instead of a module.

The four hand-written agents proved the shape: a name, a tool slice, and a
prompt that ends with the persona's shared blocks. Everything else — identity,
enrichment, triage, guardrails, the coverage post-check, disclosure, idempotency
— is inherited from the loop in `base.py` and never restated here. A bot defined
in YAML is therefore exactly as safe as one defined in Python, because the parts
that make it safe were never in the Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..contract import InboundMessage
from ..identity import ResolvedIdentity
from .base import AgentDefinition
from .blocks import _account_block, _context_block, _entry_block, _facts_block

if TYPE_CHECKING:  # bots.py imports this module; avoid the cycle at import time
    from ..bots import BotSpec


def definition_from_spec(spec: "BotSpec") -> AgentDefinition:
    prompt = spec.prompt

    if spec.persona == "dealer":

        def build_system_prompt(
            message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
        ) -> str:
            return prompt + _account_block(resolved)

    else:

        def build_system_prompt(
            message: InboundMessage, resolved: ResolvedIdentity, context: str = ""
        ) -> str:
            return prompt + _facts_block(resolved) + _context_block(context) + _entry_block(message)

    return AgentDefinition(
        name=spec.name,
        tool_names=tuple(spec.tools),
        build_system_prompt=build_system_prompt,
    )
