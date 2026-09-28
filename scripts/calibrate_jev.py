"""Run the labelled set through Jev and propose thresholds (spec §9).

    OPENROUTER_API_KEY=... python scripts/calibrate_jev.py            # report only
    OPENROUTER_API_KEY=... python scripts/calibrate_jev.py --write    # also update thresholds.yaml

Costs well under a cent for the whole set. Run by a person, never in CI. The
proposal goes into a PR like any other reviewed change.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import yaml  # noqa: E402

from emotorad_ai.calibration import CANDIDATES, evaluate_choice, evaluate_noul, suggest_choice, suggest_noul  # noqa: E402
from emotorad_ai.config import load_settings  # noqa: E402
from emotorad_ai.decisions import (  # noqa: E402
    Q_CATEGORY, Q_ERROR_CODE, Q_LANGUAGE, Q_STANDARD, Q_SUB_CATEGORY, Q_WARRANTY, THRESHOLDS_PATH,
    build_catalogue, build_questions, build_state,
)
from emotorad_ai.errorcodes import load_table  # noqa: E402
from emotorad_ai.jev import JevClient, JevError  # noqa: E402
from emotorad_ai.knowledge import KnowledgeBase  # noqa: E402
from emotorad_ai.openrouter import OpenRouterTransport  # noqa: E402
from emotorad_ai.standard_responses import load_standard_responses  # noqa: E402
from tests.jev_golden import load_golden  # noqa: E402

TARGETS = {Q_STANDARD: 0.98, Q_CATEGORY: 0.95, Q_SUB_CATEGORY: 0.90, Q_LANGUAGE: 0.95, Q_ERROR_CODE: 0.95}
EXPECTED = {
    Q_STANDARD: lambda c: c.standard_response, Q_CATEGORY: lambda c: c.category, Q_SUB_CATEGORY: lambda c: c.sub_category,
    Q_LANGUAGE: lambda c: c.language, Q_ERROR_CODE: lambda c: c.error_code,
}
YAML_KEYS = {Q_STANDARD: "standard_response", Q_CATEGORY: "category", Q_SUB_CATEGORY: "sub_category", Q_LANGUAGE: "language", Q_ERROR_CODE: "error_code"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write the proposal to knowledge/_routing/thresholds.yaml")
    args = parser.parse_args()

    settings = load_settings()
    client = JevClient(OpenRouterTransport(base_url=settings.openrouter_base_url), model=settings.jev_model, timeout=30.0)
    catalogue = build_catalogue(KnowledgeBase(), load_standard_responses(), load_table(), include_drafts=True)
    questions = build_questions(catalogue)

    pairs, failures = [], 0
    cost = 0.0
    for case in load_golden():
        try:
            decision = client.decide(build_state(case.text, [], "whatsapp", bike=case.bike or None), questions)
        except JevError as exc:
            failures += 1
            print("jev failed on %r: %s" % (case.text, exc.code))
            continue
        cost += decision.cost or 0.0
        pairs.append((case, decision))
    print("scored %d cases, %d failures, cost $%.5f\n" % (len(pairs), failures, cost))

    proposal = {}
    for question_id, target in TARGETS.items():
        if question_id not in questions:
            continue
        rows = evaluate_choice(pairs, question_id, EXPECTED[question_id], CANDIDATES)
        print("%s (target accuracy %.0f%%)" % (question_id, target * 100))
        for threshold, languages in rows.items():
            cells = ["%s %d/%d/%d" % (lang, r["correct"], r["covered"], r["n"]) for lang, r in sorted(languages.items())]
            print("  >= %.2f  %s   (correct/covered/n)" % (threshold, "  ".join(cells)))
        suggestion = suggest_choice(rows, target)
        print("  suggested: %s\n" % suggestion)
        if suggestion is not None:
            proposal[YAML_KEYS[question_id]] = suggestion

    rows = evaluate_noul(pairs, Q_WARRANTY, lambda c: c.needs_warranty_lookup, CANDIDATES)
    warranty = suggest_noul(rows, target_recall=0.95)
    print("%s suggested: %s" % (Q_WARRANTY, warranty))
    if warranty is not None:
        proposal["tools"] = {"lookup_warranty_record": warranty}

    if args.write:
        current = yaml.safe_load(THRESHOLDS_PATH.read_text(encoding="utf-8")) or {}
        current.update(proposal)
        current["calibrated_at"] = date.today().isoformat()
        current["calibration_set_size"] = len(pairs)
        THRESHOLDS_PATH.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8", newline="\n")
        print("\nwrote %s; review it in a PR" % THRESHOLDS_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
