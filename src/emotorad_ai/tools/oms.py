"""The real OMS read APIs, behind the shapes the mocks already return.

Two endpoints, both read-only:

- ``get_warranties_by_mobile``  — the purchase table. One row per registered
  bike: frame number, product, franchise, ``purchase_date`` and ``created_at``.
  This is what ``lookup_warranty_record`` reads.
- ``get_orders_by_identifiers`` — the order table. Only populated when an order
  went through OMS (website, Amazon, Flipkart, dealer app), so a franchise
  walk-in has registrations and no orders at all. Absence proves nothing: never
  read an empty result as "did not buy a bike".

**Why the mobile is validated before the call, not after.** Verified against the
live API 2026-08-29: a number with no rows returns 404 ``No Purchase data
found.`` — and so does a malformed one (``abc``, ``123``). The two are
indistinguishable in the response. Since "no record" routes a customer to Late
Warranty Registration, a normalisation bug would calmly tell a real owner they
have no bike. So anything that is not an Indian mobile never reaches the
network, and a 404 can then be trusted to mean what it says.

Auth and base URL come from the environment. The key is a credential: never
defaulted, never logged, never written to disk.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

DEFAULT_BASE_URL = "https://omsrest.emotorad.com/purchase"
DEFAULT_TIMEOUT = 8.0

_DIGITS = re.compile(r"\D+")
# Indian mobile numbers are ten digits starting 6-9. Landlines and short codes
# are not registrable identities here, so anything else is a caller bug.
_INDIAN_MOBILE = re.compile(r"^[6-9]\d{9}$")


class OMSError(Exception):
    """Base for every failure that is ours or the network's, never the customer's."""


class OMSConfigError(OMSError):
    """Missing key or URL, or a number that should never have been dialled out."""


class OMSUnavailable(OMSError):
    """The OMS could not answer. Retryable, and we say so — it is our outage."""


class OMSNoRecord(OMSError):
    """The OMS answered, and the answer is that this number has no rows.

    Distinct from OMSUnavailable on purpose: this one means "offer to register",
    the other means "come back in a minute". Collapsing them either tells a
    registered customer to re-register or an unregistered one to wait forever.
    """


def normalise_mobile(value: Optional[str]) -> str:
    """A repo-format phone (``+919876543210``) to what the API wants (``9876543210``)."""
    digits = _DIGITS.sub("", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not _INDIAN_MOBILE.match(digits):
        raise OMSConfigError("not a valid Indian mobile number: %r" % (value,))
    return digits


class OMSClient:
    """Thin read client. No caching — warranty state is not ours to go stale on."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("EMOTORAD_OMS_API_KEY", "")
        self.base_url = (base_url or os.environ.get("EMOTORAD_OMS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        # Seam for tests: the suite must stay offline, so it passes its own opener
        # rather than the module reaching for the network.
        self._opener = opener or urllib.request.urlopen

    def _get(self, path: str, params: Dict[str, str]) -> Dict[str, Any]:
        if not self.api_key:
            raise OMSConfigError("EMOTORAD_OMS_API_KEY is not set")
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request("%s/%s?%s" % (self.base_url, path, query))
        request.add_header("X-API-KEY", self.api_key)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self._raise_for_status(exc)
            raise  # unreachable: _raise_for_status always raises
        except (urllib.error.URLError, OSError) as exc:
            # DNS, TLS, reset, timeout — our side of the wire, not the customer's.
            raise OMSUnavailable("OMS unreachable: %s" % type(exc).__name__) from exc
        try:
            parsed = json.loads(body)
        except ValueError as exc:
            raise OMSUnavailable("OMS returned a non-JSON body") from exc
        if not isinstance(parsed, dict):
            raise OMSUnavailable("OMS returned %s, expected an object" % type(parsed).__name__)
        return parsed

    @staticmethod
    def _raise_for_status(exc: urllib.error.HTTPError) -> None:
        """Map status onto the outcomes the agent can act on.

        404 is the only one meaning "this customer has no rows". 400/401/403 are
        our own misconfiguration and must never reach a customer as "you own
        nothing" — from where they sit that is an outage.
        """
        if exc.code == 404:
            raise OMSNoRecord("OMS has no rows for this number")
        if exc.code in (400, 401, 403):
            raise OMSConfigError("OMS rejected the request (HTTP %d)" % exc.code)
        raise OMSUnavailable("OMS returned HTTP %d" % exc.code)

    def _rows(self, path: str, phone: str, key: str) -> List[Dict[str, Any]]:
        payload = self._get(path, {"mobile": normalise_mobile(phone)})
        rows = payload.get(key)
        if not isinstance(rows, list) or not rows:
            # A 200 carrying an empty list means what the 404 means.
            raise OMSNoRecord("OMS returned no %s rows" % key)
        return [row for row in rows if isinstance(row, dict)]

    def get_warranties_by_mobile(self, phone: str) -> List[Dict[str, Any]]:
        """Registered bikes for a number. Raises OMSNoRecord when there are none."""
        return self._rows("get_warranties_by_mobile", phone, "purchases")

    def get_orders_by_identifiers(self, phone: str) -> List[Dict[str, Any]]:
        """OMS orders for a number. Raises OMSNoRecord for a franchise walk-in."""
        return self._rows("get_orders_by_identifiers", phone, "orders")

    def get_orders_by_code(self, code: str) -> List[Dict[str, Any]]:
        """Orders for an order code or an invoice code, whichever the customer read out.

        Both are printed on the invoice and a customer will not know which is
        which, so both are tried. Verified 2026-08-29: ``order_code`` and
        ``invoice_code`` are accepted parameters; ``invoice_no`` and
        ``docket_number`` are not.

        This exists to recover the registered phone for someone who cannot recall
        it. An order code is **not a secret** — it is printed on paper and
        emailed — so what the caller does with the result matters more than this
        lookup: it must never be treated as proof of identity.
        """
        code = (code or "").strip()
        if not code:
            raise OMSConfigError("no order or invoice code supplied")
        last: Exception = OMSNoRecord("no rows")
        for param in ("order_code", "invoice_code"):
            try:
                payload = self._get("get_orders_by_identifiers", {param: code})
            except OMSNoRecord as exc:
                last = exc
                continue
            rows = payload.get("orders")
            if isinstance(rows, list) and rows:
                return [row for row in rows if isinstance(row, dict)]
        raise last
