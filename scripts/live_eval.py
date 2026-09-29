"""Run the live evaluation on OpenRouter: Jev and Haiku, for real.

    python scripts/live_eval.py --list              # show the scenarios; sends nothing
    python scripts/live_eval.py --budget 3          # run everything, stop at $3
    python scripts/live_eval.py --only guardrails   # one family, or one scenario id
    python scripts/live_eval.py --repeat 3          # each scenario three times

Spec: docs/superpowers/specs/2026-09-29-live-openrouter-eval-design.md.
Reads OPENROUTER_API_KEY (never printed). Fixture people only, conversations in
memory, business tools mocked: nothing is written to any database. Writes
reports/live-eval/<UTC time>/report.html and results.json. A Claude session
runs it only after the person says yes.
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.config import Settings  # noqa: E402
from emotorad_ai.live_eval.report import write_report  # noqa: E402
from emotorad_ai.live_eval.runner import run_suite, summarise  # noqa: E402
from emotorad_ai.live_eval.scenarios import DEFAULT_PATH, ScenarioError, known_from_code, load_suite  # noqa: E402
from emotorad_ai.openrouter import OpenRouterConfigError  # noqa: E402
from emotorad_ai.wiring import build_models  # noqa: E402


def main(argv=None, models_factory=build_models, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description="Live evaluation on OpenRouter.")
    parser.add_argument("--list", action="store_true", help="show the scenarios and send nothing")
    parser.add_argument("--only", action="append", help="a family or a scenario id; repeat to name several")
    parser.add_argument("--budget", type=float, default=3.0, help="stop before the next scenario once this many dollars are spent")
    parser.add_argument("--repeat", type=int, default=1, help="run each scenario this many times")
    parser.add_argument("--pause", type=float, default=20.0, help="seconds to wait before retrying a provider error")
    parser.add_argument("--scenarios", default=str(DEFAULT_PATH), help=argparse.SUPPRESS)
    parser.add_argument("--out", default=str(ROOT / "reports" / "live-eval"), help="where reports are written")
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.budget <= 0:
        parser.error("--repeat must be 1 or more and --budget above 0")

    try:
        suite = load_suite(Path(args.scenarios), known_from_code())
        scenarios = suite.select(args.only)
    except ScenarioError as exc:
        print("scenario file: %s" % exc)
        return 2

    if args.list:
        for s in scenarios:
            print("%-12s %-42s %d turn(s)" % (s.family, s.id, len(s.turns)))
        print("%d scenario(s). Nothing was sent." % len(scenarios))
        return 0

    def progress(outcome):
        # The unknown count says how far the dollar figure undercounts.
        spend = outcome.spend
        print("%-8s %-12s %-42s $%.4f, %d call(s) of unknown cost" % (
            outcome.final.status.upper(), outcome.scenario.family, outcome.scenario.id, spend.total, spend.unknown),
            flush=True)

    try:
        run = run_suite(scenarios, Settings(), budget=args.budget, repeat=args.repeat,
                        models_factory=models_factory, pause=args.pause, sleep=sleep, progress=progress)
    except OpenRouterConfigError as exc:
        print("cannot start: %s" % exc)
        return 2

    out_dir = Path(args.out) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    html_path, _ = write_report(run, suite.mixes, out_dir)
    summaries = summarise(run)
    counts = {status: sum(1 for s in summaries if s.status == status) for status in ("pass", "flaky", "fail", "provider")}
    print("%s. Spent $%.4f." % (", ".join("%d %s" % (n, status) for status, n in counts.items()), run.spend.total))
    if run.skipped:
        print("Not run, budget reached: %s" % ", ".join(run.skipped))
    if run.aborted:
        print("Stopped: %s" % run.aborted)
    print("Report: %s" % html_path)
    if run.aborted:
        return 2
    return 0 if counts["pass"] == len(summaries) and not run.skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
