"""One real Decisions API call, to confirm the live response shape (spec §3.1, §11).

    OPENROUTER_API_KEY=... python scripts/jev_probe.py

Saves the questions and the raw response to docs/api-shapes/jev-decisions.json.
The state is a fixed, made-up sentence, so no customer data is sent or saved.
tests/test_jev.py then parses the saved file on every run.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.decisions import CATEGORY_CRITERIA, CATEGORY_INSTRUCTIONS, Q_CATEGORY, Q_WARRANTY, WARRANTY_INSTRUCTIONS  # noqa: E402
from emotorad_ai.jev import choice, noul, parse_decision  # noqa: E402
from emotorad_ai.openrouter import DECISIONS_PATH, OpenRouterTransport  # noqa: E402

OUT = ROOT / "docs" / "api-shapes" / "jev-decisions.json"


def main() -> int:
    settings = load_settings()
    questions = {
        Q_CATEGORY: choice(CATEGORY_INSTRUCTIONS, CATEGORY_CRITERIA),
        Q_WARRANTY: noul(WARRANTY_INSTRUCTIONS, true="Asks about warranty or cost.", false="Does not."),
    }
    body = {
        "model": settings.jev_model,
        "state": {"message": "my battery is not charging, is it under warranty?"},
        "questions": {qid: q.to_dict() for qid, q in questions.items()},
    }
    response = OpenRouterTransport(base_url=settings.openrouter_base_url).post(DECISIONS_PATH, body, timeout=30.0)
    decision = parse_decision(response, questions)
    OUT.write_text(
        json.dumps({"questions": body["questions"], "response": response}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    print("parsed OK: %s" % decision.scores())
    print("saved %s" % OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
