"""Turn a saved test-console run into a static, chat-like report.

    python scripts/e2e_report.py logs/e2e/run-20260929-165605.json --out logs/e2e/report.html
    python scripts/e2e_report.py logs/e2e/run-....json --only smoke-typed --out logs/e2e/smoke-typed.html

The console (/dev/e2e) saves every run to logs/e2e/. This page needs no server
and no login, so it can be opened anywhere, attached to a ticket, or captured
with a headless browser for screenshots (one scenario per page with --only).
The photo is linked, not embedded: pass --photo with a path relative to the
report (default: the sample the console sends).
"""

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PHOTO = ROOT / "tests" / "data" / "live_media" / "smoke-battery.jpg"

STYLE = """
  :root { --ink:#171717; --muted:#666; --accent:#FF6B2B; --bg:#FAFAFA; --card:#fff; --border:#EAEAEA;
          --ai:#F1F1F1; --user:#171717; --pass:#1E7D3A; --pass-bg:#E8F5EC; --fail:#B3261E; --fail-bg:#FDECEA; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 Figtree, "Segoe UI", system-ui, sans-serif; }
  .wrap { max-width: 900px; margin: 0 auto; padding: 20px 16px 40px; }
  h1 { font-size: 20px; margin: 0 0 4px; } .sub { color: var(--muted); margin: 0 0 16px; }
  section { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 14px; margin-bottom: 16px; }
  h2 { font-size: 15px; margin: 0 0 4px; display: flex; gap: 8px; align-items: center; }
  .why { color: var(--muted); margin: 0 0 10px; }
  .status { font-size: 11px; font-weight: 700; border-radius: 999px; padding: 2px 8px; }
  .status.pass { background: var(--pass-bg); color: var(--pass); } .status.finding { background: var(--fail-bg); color: var(--fail); }
  .row { display: flex; margin: 6px 0; } .row.user { justify-content: flex-end; }
  .bubble { max-width: 78%; padding: 10px 13px; border-radius: 16px; white-space: pre-line; overflow-wrap: anywhere; }
  .row.ai .bubble { background: var(--ai); border-top-left-radius: 4px; }
  .row.user .bubble { background: var(--user); color: #fff; border-top-right-radius: 4px; }
  .row.user img { max-width: 240px; border-radius: 14px; display: block; }
  .check { border: 1px dashed var(--border); border-radius: 12px; padding: 8px 10px; margin: 2px 0 12px; font-size: 12.5px; }
  .meta { color: var(--muted); font: 12px ui-monospace, Consolas, monospace; overflow-wrap: anywhere; margin-bottom: 4px; }
  ul { margin: 0; padding-left: 18px; } .ok { color: var(--pass); } .bad { color: var(--fail); font-weight: 600; }
  #findings li { margin-bottom: 6px; }
"""


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _findings(results: Sequence[Dict[str, Any]]) -> List[str]:
    lines = []
    for result in results:
        for number, step in enumerate(result.get("steps") or [], start=1):
            for check in step.get("checks") or []:
                if not check.get("ok"):
                    got = ' <span class="meta">(got: %s)</span>' % _e(check.get("got")) if check.get("got") else ""
                    lines.append("<li><b>%s</b>, step %d: %s%s</li>" % (_e(result.get("title")), number, _e(check.get("label")), got))
    return lines


def _step(step: Dict[str, Any], photo: str) -> str:
    out = []
    if step.get("photo"):
        out.append('<div class="row user"><img src="%s" alt="The photo the customer sent"></div>' % _e(photo))
    if step.get("sent"):
        out.append('<div class="row user"><div class="bubble">%s</div></div>' % _e(step["sent"]))
    reply = step.get("reply") or {}
    out.append('<div class="row ai"><div class="bubble">%s</div></div>' % _e(reply.get("text")))
    meta = "via %s" % _e(reply.get("handled_by"))
    if reply.get("escalated"):
        meta += " · handed to a person"
    if reply.get("ticket_id"):
        meta += " · ticket %s" % _e(reply["ticket_id"])
    if step.get("ms") is not None:
        meta += " · %s ms" % _e(step["ms"])
    media = step.get("media") or []
    if media:
        record = media[-1]
        meta += "<br>media: %s (%s, %s bytes)" % (_e(record.get("uri")), _e(record.get("source")), _e(record.get("size_bytes")))
    checks = "".join(
        '<li class="%s">%s %s%s</li>' % (
            "ok" if c.get("ok") else "bad", "&#10003;" if c.get("ok") else "&#10007;", _e(c.get("label")),
            "" if c.get("ok") else " (got: %s)" % _e(c.get("got")))
        for c in step.get("checks") or [])
    out.append('<div class="check"><div class="meta">%s</div><ul>%s</ul></div>' % (meta, checks))
    return "".join(out)


def render(run: Dict[str, Any], photo: str, only: Optional[str] = None) -> str:
    results = [r for r in run.get("results") or [] if only is None or r.get("id") == only]
    everything = run.get("results") or []
    passed = sum(1 for r in everything if r.get("status") == "pass")
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             "<title>Chat Test Report</title><style>%s</style></head><body><div class=\"wrap\">" % STYLE,
             "<h1>Support chat · end-to-end test report</h1>",
             '<p class="sub">%s · %s · %d of %d scenarios passed</p>' % (
                 _e(run.get("at")), _e(run.get("server")), passed, len(everything))]
    if only is None:
        lines = _findings(everything)
        parts.append('<section><h2>Findings</h2><ul id="findings">%s</ul></section>' % (
            "".join(lines) or '<li class="ok">No findings: every check passed.</li>'))
    for result in results:
        status = result.get("status") or "pending"
        parts.append('<section><h2>%s <span class="status %s">%s</span></h2><p class="why">%s</p>'
                     '<div class="meta">conversation %s</div>%s</section>' % (
                         _e(result.get("title")), _e(status), _e(status), _e(result.get("why")),
                         _e(result.get("conversation_id")),
                         "".join(_step(step, photo) for step in result.get("steps") or [])))
    parts.append("</div></body></html>")
    return "".join(parts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A saved test-console run as a static report.")
    parser.add_argument("run", help="a run saved by the console, logs/e2e/run-*.json")
    parser.add_argument("--out", required=True, help="the HTML file to write")
    parser.add_argument("--photo", default=None, help="the photo's path relative to the report")
    parser.add_argument("--only", default=None, help="one scenario id, for a screenshot of it alone")
    args = parser.parse_args(argv)
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    out = Path(args.out)
    photo = args.photo or Path(DEFAULT_PHOTO).resolve().as_uri()
    out.write_text(render(run, photo, args.only), encoding="utf-8")
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
