"""The active base prompt for each agent, as a reviewable file.

Why this exists
---------------
There were two prompts and only one of them ran. The playground tunes a base
prompt and `scripts/publish_prompt.py` appends each version to
`.playground/prompts/<agent>.json`; by v28 that text was 22,162 characters and
carried every rule added since. The prompt the service actually ran was the
2,641-character literal in `agents/battery_support.py`, which had none of them.
Nothing was wrong with either file. The step between them, a human copying the
agreed text into the module, had simply not been run since v24, and nothing
announced that.

So the published store stays what it is, a tuning history, and the *active*
prompt becomes one plain file per agent under `prompts/`. `promote_prompt.py`
moves a chosen version from the store into it and prints the diff. That keeps
the review gate `playground._save_diff` was written to serve: the live prompt
changes in a pull request, not the moment somebody clicks Save in a sandbox.

Loading rather than pasting
---------------------------
The module keeps its literal as a fallback, so a checkout with no `prompts/`
directory still works and the tests that predate this still pass. When the file
is present it wins. Assignment happens once at import and the module global is
what `build_system_prompt` reads, so the playground's `_tuned_system_prompt`
monkey-patch is unaffected: it substitutes the same global for the duration of
one call, exactly as before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def prompt_path(agent_name: str) -> Path:
    return PROMPT_DIR / ("%s.md" % agent_name)


def load_base_prompt(agent_name: str, directory: Optional[Path] = None) -> Optional[str]:
    """The promoted base prompt for one agent, or None to use the module's own.

    Returns None rather than raising on a missing file: an agent that has never
    been promoted is a normal state, not a broken deployment. An empty or
    whitespace-only file *is* suspicious, though, and also returns None rather
    than handing the model nothing to work from.
    """
    path = (Path(directory) / ("%s.md" % agent_name)) if directory else prompt_path(agent_name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None
