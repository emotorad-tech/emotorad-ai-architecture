"""The warranty step, run by code once the issue is verified (spec
2026-10-09 warranty step).

Four cases for the chosen bike: a purchase date (cover is computed), no date
with an invoice on file (code reads it), no date and no invoice (ask for
one), and no frame on record (registration comes in the app; a placeholder
line and a register_warranty button for now). Until the step has run for a
bike, the agent sees the bike without its cover (pending_view).

The Hindi lines are drafts for a Hindi speaker to check.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .triage import bike_ref

DATED = "dated"
INVOICE_ON_FILE = "invoice_on_file"
NEEDS_INVOICE = "needs_invoice"
NO_FRAME = "no_frame"
NO_BIKE = "-"
AFTER_ISSUE = "after_issue"

CHECKING_INVOICE_LINE = "I'm checking the invoice we have on file for your bike."
CHECKING_INVOICE_LINE_HI = "मैं आपकी बाइक के लिए हमारे पास मौजूद इनवॉइस देख रहा हूँ।"  # DRAFT
NEEDS_INVOICE_LINE = ("To check your warranty, please send a clear photo or PDF of your purchase invoice "
                      "showing the date.")
NEEDS_INVOICE_LINE_HI = ("वारंटी जाँचने के लिए, कृपया अपने खरीद इनवॉइस की साफ़ फ़ोटो या PDF भेजें, "
                         "जिसमें तारीख दिखे।")  # DRAFT
WITH_SUPPORT_LINE = "Your invoice is already with our support team, who will confirm your warranty."
WITH_SUPPORT_LINE_HI = "आपका इनवॉइस पहले से हमारी सपोर्ट टीम के पास है, वे आपकी वारंटी की पुष्टि करेंगे।"  # DRAFT
REGISTER_LATER_LINE = ("Your bike isn't registered with us yet. You'll be able to register its warranty "
                       "in the app soon.")
REGISTER_LATER_LINE_HI = "आपकी बाइक अभी हमारे पास रजिस्टर नहीं है। जल्द ही आप ऐप में इसकी वारंटी रजिस्टर कर पाएँगे।"  # DRAFT
REGISTER_ACTION = {"kind": "register_warranty", "label": "Register warranty"}
NO_BIKES_HELP = ("I couldn't find a bike registered on this number. Tell me what is happening with your bike "
                 "and I'll help.")
AFTER_ISSUE_NOTE = ("The warranty is checked by the platform once the fault is confirmed. Do not state, "
                    "estimate or look up coverage yet; help with the issue first.")

# Fields that say anything about cover, dropped from the agent's view of a
# bike until the step has run for it.
_COVER_FIELDS = ("in_warranty", "months_remaining", "warranty_start", "warranty_end", "warranty_start_source",
                 "purchase_date", "components", "term_months", "term_source", "invoice_on_file",
                 "invoice_with_support", "remedy", "note", "coverage", "warranty_api")

# Words that ask to register, matched as substrings of the lower-cased text
# (never \b or \w: Devanagari vowel signs fall outside \w).
_REGISTER_WORDS = ("register", "registration", "रजिस्टर", "पंजीकरण")
_WARRANTY_OR_BIKE = ("warranty", "warrenty", "bike", "cycle", "वारंटी", "बाइक", "साइकिल")


def chosen_bike(resolved: Any, state: Any) -> Optional[Dict[str, Any]]:
    if not state.selected_frame:
        return None
    for bike in resolved.bikes:
        if bike_ref(bike) == state.selected_frame:
            return bike
    return None


def case_of(resolved: Any, state: Any) -> Optional[str]:
    """The chosen bike's case, or None while the rider has bikes and none is chosen."""
    if state.unlisted_bike or resolved.method == "no_warranty_record" or not resolved.bikes:
        return NO_FRAME
    bike = chosen_bike(resolved, state)
    if bike is None:
        return None
    if bike.get("coverage_status") == "purchase_date_missing":
        return INVOICE_ON_FILE if bike.get("invoice_on_file") else NEEDS_INVOICE
    return DATED


def line_for(case: str, bike: Optional[Dict[str, Any]], hindi: bool) -> Optional[str]:
    if case == INVOICE_ON_FILE:
        return CHECKING_INVOICE_LINE_HI if hindi else CHECKING_INVOICE_LINE
    if case == NEEDS_INVOICE:
        if (bike or {}).get("invoice_with_support"):
            return WITH_SUPPORT_LINE_HI if hindi else WITH_SUPPORT_LINE
        return NEEDS_INVOICE_LINE_HI if hindi else NEEDS_INVOICE_LINE
    if case == NO_FRAME:
        return REGISTER_LATER_LINE_HI if hindi else REGISTER_LATER_LINE
    return None


def actions_for(case: str) -> List[Dict[str, str]]:
    return [dict(REGISTER_ACTION)] if case == NO_FRAME else []


def pending_view(bike: Dict[str, Any]) -> Dict[str, Any]:
    view = {key: value for key, value in bike.items() if key not in _COVER_FIELDS}
    view["coverage_status"] = AFTER_ISSUE
    return view


def asks_to_register(text: str) -> bool:
    """A request to register: a registration word on its own, or with a
    warranty or bike word. Any script; substrings only."""
    said = (text or "").lower().strip()
    if not any(word in said for word in _REGISTER_WORDS):
        return False
    return said in _REGISTER_WORDS or any(word in said for word in _WARRANTY_OR_BIKE) or "करना" in said
