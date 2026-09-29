"""The live-eval report: one HTML page a person reads, and results.json.

Spec: docs/superpowers/specs/2026-09-29-live-openrouter-eval-design.md, section 4.3.
The page is rendered from the same dict as results.json, so the two always
agree. Everything a model wrote is escaped: a reply is data, never markup.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .runner import Attempt, SuiteRun, project, summarise
from .scenarios import FAMILIES

STATUSES = ("pass", "flaky", "fail", "provider")

_CSS = """
:root{--bg:#fbfaf7;--fg:#1f1d1a;--muted:#6b665e;--line:#e4e0d8;--card:#ffffff;--pass:#1f7a4d;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme: dark){:root{--bg:#161513;--fg:#ecebe7;--muted:#a19c93;--line:#34312c;--card:#1f1e1b;--pass:#5cc08b;--bad:#f08a80;--warn:#e0b25c}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:900px;margin:0 auto;padding:24px 16px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:24px 0 8px}h3{font-size:15px;margin:16px 0 4px}
table{border-collapse:collapse;margin:8px 0 16px}th,td{border-bottom:1px solid var(--line);padding:4px 12px;text-align:left}
.scenario{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:16px 0}
.badge{font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;font-weight:500}
.s-pass .badge{color:var(--pass)}.s-fail .badge,.bad{color:var(--bad)}.s-flaky .badge,.s-provider .badge,.warn{color:var(--warn)}
.turn{border-top:1px solid var(--line);padding:8px 0}
pre{white-space:pre-wrap;word-break:break-word;margin:0 0 8px;font:inherit}
.who{margin:4px 0 0;font-weight:600}.meta,.notes{color:var(--muted);font-size:13px;margin:4px 0}
"""


def _attempt(attempt: Attempt) -> Dict[str, Any]:
    return {
        "status": attempt.status,
        "error": attempt.error,
        "cost": attempt.cost.to_dict(),
        "turns": [
            {
                "text": t.text, "reply": t.reply, "handled_by": t.handled_by, "path": t.path,
                "sub_category": t.sub_category, "tools": t.tools, "ticket_id": t.ticket_id,
                "escalated": t.escalated, "media": t.media, "cost": t.cost.to_dict(), "seconds": t.seconds,
                "failures": t.failures, "provider_codes": t.provider_codes,
                "blocked_reason": t.blocked_reason, "suppressed": t.suppressed,
            }
            for t in attempt.turns
        ],
    }


def results(run: SuiteRun, mixes: Mapping[str, Mapping[str, int]]) -> Dict[str, Any]:
    summaries = summarise(run)
    return {
        "started_at": run.started_at,
        "models": run.models,
        "budget": run.budget,
        "repeat": run.repeat,
        "aborted": run.aborted,
        "skipped": list(run.skipped),
        "spend": run.spend.to_dict(),
        "projection": project(summaries, mixes),
        "scenarios": [
            {
                "id": s.scenario.id, "family": s.scenario.family, "note": s.scenario.note,
                "status": s.status, "runs": s.runs, "passes": s.passes, "cost": s.cost,
                "outcomes": [[_attempt(a) for a in o.attempts] for o in run.outcomes if o.scenario.id == s.scenario.id],
            }
            for s in summaries
        ],
    }


def _money(value: Optional[float]) -> str:
    return "not measured" if value is None else "$%.4f" % value


def render_html(data: Mapping[str, Any]) -> str:
    e = html.escape
    parts: List[str] = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Live evaluation</title><style>%s</style></head><body><main>" % _CSS,
        "<h1>Live evaluation</h1>",
        "<p class='meta'>Started %s. Jev %s, narrow %s, full %s. Budget $%.2f, %d run(s) per scenario.</p>" % (
            e(data["started_at"]), e(data["models"]["jev"]), e(data["models"]["narrow"]), e(data["models"]["full"]),
            data["budget"], data["repeat"]),
    ]
    if data["aborted"]:
        parts.append("<p class='bad'>Stopped: %s</p>" % e(data["aborted"]))
    if data["skipped"]:
        parts.append("<p class='warn'>Not run, budget reached: %s</p>" % e(", ".join(data["skipped"])))

    counts = {family: {status: 0 for status in STATUSES} for family in FAMILIES}
    for s in data["scenarios"]:
        counts[s["family"]][s["status"]] += 1
    rows = "".join(
        "<tr><th>%s</th>%s</tr>" % (family, "".join("<td>%d</td>" % counts[family][status] for status in STATUSES))
        for family in FAMILIES if any(counts[family].values())
    )
    parts.append("<h2>Results</h2><table><tr><th>Family</th>%s</tr>%s</table>" % (
        "".join("<th>%s</th>" % status for status in STATUSES), rows))

    spend = data["spend"]
    parts.append("<h2>Spend</h2><p>$%.4f over %d model call(s).</p>" % (spend["total"], spend["calls"]))
    if spend["unknown"]:
        parts.append("<p class='warn'>%d call(s) have no billed cost (none came back, or the call failed); "
                     "the total is a lower bound.</p>" % spend["unknown"])
    parts.append("<table>%s</table>" % "".join(
        "<tr><th>%s</th><td>$%.4f</td></tr>" % (e(model), amount) for model, amount in spend["by_model"].items()))

    parts.append("<h2>Projected cost</h2><table><tr><th>Mix</th><th>10 conversations</th><th>Per 1,000</th></tr>")
    for name, projected in data["projection"].items():
        if projected["missing"]:
            cells = "<td colspan='2'>not measured: %s</td>" % e(", ".join(projected["missing"]))
        else:
            cells = "<td>$%.4f</td><td>$%.2f</td>" % (projected["ten"], projected["per_1000"])
        parts.append("<tr><th>%s</th>%s</tr>" % (e(name), cells))
    parts.append("</table>")

    for s in data["scenarios"]:
        parts.append("<section class='scenario s-%s'><h2>%s <span class='badge'>%s</span></h2>" % (s["status"], e(s["id"]), s["status"]))
        parts.append("<p class='meta'>%s. %d of %d run(s) passed. %s per conversation.</p>" % (
            e(s["family"]), s["passes"], s["runs"], _money(s["cost"])))
        if s["note"]:
            parts.append("<p>%s</p>" % e(s["note"]))
        for number, attempts in enumerate(s["outcomes"], start=1):
            for tries, attempt in enumerate(attempts, start=1):
                label = "Run %d%s" % (number, ", retried after a provider error" if tries > 1 else "")
                parts.append("<h3>%s: %s</h3>" % (label, e(attempt["status"])))
                if attempt["error"]:
                    parts.append("<p class='bad'>%s</p>" % e(attempt["error"]))
                for t in attempt["turns"]:
                    parts.append("<div class='turn'><p class='who'>Customer</p><pre>%s</pre><p class='who'>Bot</p><pre>%s</pre>" % (
                        e(t["text"]), e(t["reply"])))
                    parts.append("<p class='meta'>path %s, %s, record %s, tools %s, ticket %s, $%.4f, %.1f s</p>" % (
                        e(t["path"]), e(t["handled_by"]), e(t["sub_category"] or "none"), e(", ".join(t["tools"]) or "none"),
                        e(t["ticket_id"] or "none"), t["cost"]["total"], t["seconds"]))
                    if t["failures"]:
                        parts.append("<ul class='bad'>%s</ul>" % "".join("<li>%s</li>" % e(f) for f in t["failures"]))
                    if t["suppressed"]:
                        parts.append("<p class='warn'>Blocked by %s (%s). The model wrote:</p><pre>%s</pre>" % (
                            e(t["handled_by"]), e(t["blocked_reason"] or "no reason given"), e(t["suppressed"])))
                    if t["provider_codes"]:
                        parts.append("<p class='warn'>provider: %s</p>" % e(", ".join(t["provider_codes"])))
                    parts.append("<p class='notes'>Wording notes: ______________________________</p></div>")
        parts.append("</section>")
    parts.append("</main></body></html>")
    return "".join(parts)


def write_report(run: SuiteRun, mixes: Mapping[str, Mapping[str, int]], out_dir: Path) -> Tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = results(run, mixes)
    json_path = out_dir / "results.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    html_path = out_dir / "report.html"
    html_path.write_text(render_html(data), encoding="utf-8", newline="\n")
    return html_path, json_path
