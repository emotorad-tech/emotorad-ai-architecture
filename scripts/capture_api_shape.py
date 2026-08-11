#!/usr/bin/env python3
"""Capture the *shape* of an OMS API response without capturing anyone's data.

Calls the warranty and orders endpoints, then replaces every value with a
structure-preserving mask — digits become 9, letters become A — so field names,
nesting, types, nulls and formats survive while the content does not. The output
is safe to commit, paste into a chat, or hand to a contractor.

    export OMS_API_KEY='...'          # never hard-code it, never commit it
    python3 scripts/capture_api_shape.py 9876543210

Writes docs/api-shapes/{warranty,orders}.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://omsrest.emotorad.com/purchase"
ENDPOINTS = {
    "warranty": "get_warranties_by_mobile?mobile={phone}",
    "orders": "get_orders_by_identifiers?mobile={phone}",
}
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "api-shapes"

# Field names whose *values* are structural rather than personal — keeping them
# readable makes the shape far more useful (status machines, enums, type codes).
KEEP = re.compile(
    r"status|type|source|category|state$|_flag|is_|has_|priority|currency|unit",
    re.IGNORECASE,
)


def mask(value, key=""):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # Preserve magnitude, not the number itself.
        return int("9" * len(str(abs(int(value))))) if value else 0
    if isinstance(value, str):
        if KEEP.search(key):
            return value  # enum-like, safe and informative
        if len(value) > 60:
            return f"<text len~{len(value)}>"
        return re.sub(r"[0-9]", "9", re.sub(r"[A-Za-z]", "A", value))
    if isinstance(value, list):
        # One representative element is enough to show the shape.
        return [mask(value[0], key)] + [f"<+{len(value) - 1} more>"] if value else []
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    return f"<{type(value).__name__}>"


def fetch(url: str, api_key: str):
    request = urllib.request.Request(url, headers={"X-API-KEY": api_key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        # The error shape matters as much as the success shape — it is how the
        # agent tells "no record" apart from "the OMS is down".
        body = exc.read().decode()[:2000]
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body}


def main() -> int:
    api_key = os.environ.get("OMS_API_KEY")
    if not api_key:
        print("Set OMS_API_KEY first:  export OMS_API_KEY='...'", file=sys.stderr)
        return 1
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    phone = sys.argv[1]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for name, path in ENDPOINTS.items():
        status, payload = fetch(f"{BASE}/{path.format(phone=phone)}", api_key)
        shape = {"_http_status": status, "_endpoint": path.split("?")[0], "body": mask(payload)}
        out = OUT_DIR / f"{name}.json"
        out.write_text(json.dumps(shape, indent=2))
        print(f"{name:9} HTTP {status}  →  {out.relative_to(OUT_DIR.parent.parent)}")

    print("\nRun again with a phone number that has NO record — the difference between")
    print("'no record' and 'call failed' is what routes a customer to Late Warranty")
    print("Registration rather than telling them we're down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
