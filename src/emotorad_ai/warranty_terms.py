"""Warranty from the purchase date, per part (the person's terms, 8 October 2026).

The one place the terms live. Cover starts on `purchase_date` (never
`created_at`, which is when the customer registered) and each part runs its
own term: `valid_until` is the day before the same calendar date `months`
later, and the last day is covered. The bike's status follows the battery.
The result has the shape the warranty service's coverage had
(tools/mocks._api_coverage reads it), so the agents and the post-checks read
it unchanged.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Dict, Optional

TERMS: Dict[str, int] = {"display": 6, "charger": 6, "controller": 12, "battery": 12, "motor": 12, "frame": 60}
LEAD_PART = "battery"


def _add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def valid_until(start: date, months: int) -> date:
    """The last covered day: the day before the same date `months` later."""
    return _add_months(start, months) - timedelta(days=1)


def coverage(purchase_date: Optional[date], today: date) -> Dict[str, Any]:
    """Each part's cover from `purchase_date`; "unknown" with the remedy
    without one."""
    if purchase_date is None:
        return {"status": "unknown", "remedy": "collect_purchase_proof", "components": []}
    components = []
    for part, months in TERMS.items():
        end = valid_until(purchase_date, months)
        components.append({"component": part, "months": months, "validUntil": end.isoformat(),
                           "active": today <= end})
    lead = next(part for part in components if part["component"] == LEAD_PART)
    return {"status": "active" if lead["active"] else "expired", "remedy": None, "components": components}
