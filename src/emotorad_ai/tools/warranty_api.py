"""The warranty API: the rider's registered cycles and their coverage, by phone.

Sachin's warranty service (7 October 2026) replaces the OMS warranty lookup for
the bot. One server-to-server call, ``searchRegistrationsByPhone``:

    POST /api/warranty/v1/service/ai/registrations/search
    x-service-key: <the AI service key>
    {"phone": "+91...", "country": "IN"}

It answers a list, one item per registered cycle: ``frameNumber``,
``productName``, ``purchaseDate``, ``status`` (``active`` or
``pending_review``) and ``coverage`` (``status`` unknown, active or expired;
``remedy`` ``collect_purchase_proof`` when the date is missing; and one entry
per part with ``months``, ``validUntil`` and ``active``). No personal fields.
The spec is docs/api-shapes/warranty-ai-openapi.json and its example is
docs/api-shapes/warranty-ai-search.json.

Unlike the OMS, the service computes coverage itself, per part, so the bot
quotes its dates rather than adding a provisional term to a purchase date.

The key is a credential: never defaulted, never logged, never in an error
message or a repr. The base URL defaults to staging, the only deployment the
spec names today; production sets ``EMOTORAD_WARRANTY_API_URL``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .oms import OMSConfigError, normalise_mobile

KEY_ENV = "EMOTORAD_WARRANTY_API_KEY"
URL_ENV = "EMOTORAD_WARRANTY_API_URL"
DEFAULT_BASE_URL = "https://d2c-storefront-staging.emotorad.com"
SEARCH_PATH = "/api/warranty/v1/service/ai/registrations/search"
DEFAULT_TIMEOUT = 8.0
DEFAULT_COUNTRY = "IN"


class WarrantyAPIError(Exception):
    """Base for every failure that is ours or the network's, never the customer's."""


class WarrantyAPIConfigError(WarrantyAPIError):
    """No key, a number that should never have been dialled out, or the service
    refusing our request (400, 401, 403): our misconfiguration, so an outage."""


class WarrantyAPIUnavailable(WarrantyAPIError):
    """The service could not answer. Retryable, and we say so: it is our outage."""


class WarrantyAPINoRecord(WarrantyAPIError):
    """The service answered, and this number has no registrations.

    Distinct from WarrantyAPIUnavailable on purpose: this one means "offer to
    register", the other "come back in a minute".
    """


def configured() -> bool:
    return bool(os.environ.get(KEY_ENV))


class WarrantyAPIClient:
    """Thin read client. No caching: coverage is not ours to go stale on."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        country: str = DEFAULT_COUNTRY,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(KEY_ENV, "")
        self.base_url = (base_url or os.environ.get(URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.country = country
        # Seam for tests: the suite stays offline by passing its own opener.
        self._opener = opener or urllib.request.urlopen

    def __repr__(self) -> str:
        return "WarrantyAPIClient(base_url=%r, key=%s)" % (self.base_url, "set" if self._api_key else "missing")

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.base_url).netloc or self.base_url

    def search(self, phone: Optional[str]) -> List[Dict[str, Any]]:
        """Registered cycles for a number. Raises WarrantyAPINoRecord when there are none."""
        if not self._api_key:
            raise WarrantyAPIConfigError("%s is not set" % KEY_ENV)
        try:
            # The same check as the OMS client: anything not an Indian mobile is
            # a caller bug and never reaches the network.
            digits = normalise_mobile(phone)
        except OMSConfigError as exc:
            raise WarrantyAPIConfigError(str(exc)) from exc
        body = json.dumps({"phone": "+91" + digits, "country": self.country}).encode("utf-8")
        request = urllib.request.Request(self.base_url + SEARCH_PATH, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("x-service-key", self._api_key)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise self._for_status(exc) from None
        except (urllib.error.URLError, OSError) as exc:
            # DNS, TLS, reset, timeout: our side of the wire, not the customer's.
            raise WarrantyAPIUnavailable("warranty API unreachable: %s" % type(exc).__name__) from None
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise WarrantyAPIUnavailable("warranty API returned a non-JSON body") from None
        if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
            raise WarrantyAPIUnavailable("warranty API returned %s, expected a list of objects" % type(parsed).__name__)
        if not parsed:
            raise WarrantyAPINoRecord("no registrations for this number")
        return parsed

    @staticmethod
    def _for_status(exc: urllib.error.HTTPError) -> WarrantyAPIError:
        """Every refusal is ours, so none may reach a customer as "you own nothing".

        The spec answers an unregistered number with 200 and an empty list, so
        no status here means "no record". 400 (the number was checked before
        the call), 401 and 403 are our misconfiguration; 429 and 5xx are an
        outage.
        """
        code = _error_code(exc)
        if exc.code in (400, 401, 403):
            return WarrantyAPIConfigError("warranty API rejected the request (HTTP %d %s)" % (exc.code, code))
        return WarrantyAPIUnavailable("warranty API returned HTTP %d %s" % (exc.code, code))


def _error_code(exc: urllib.error.HTTPError) -> str:
    """The spec's error code (``{"error": {"code": ...}}``), for the log only."""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        code = payload.get("error", {}).get("code")
        return str(code) if code else ""
    except Exception:  # noqa: BLE001 - a body we cannot read changes nothing
        return ""


def to_record(item: Dict[str, Any]) -> Dict[str, Any]:
    """One registration as the record the lookup tool reads.

    The OMS record's field names, so the tool, the agents and the post-checks
    read it unchanged; the service's own coverage rides along under
    ``warranty_api`` and is what ``tools.mocks._coverage`` answers from.
    """
    return {
        "frame_number": item.get("frameNumber"),
        "product_name": item.get("productName"),
        "purchase_date": item.get("purchaseDate"),
        "registration_status": item.get("status"),
        "warranty_api": item.get("coverage") if isinstance(item.get("coverage"), dict) else {},
    }


def api_warranty_source(client: Any) -> Callable[[str], Optional[List[Dict[str, Any]]]]:
    """Registered bikes from the warranty API, mapped onto the tool's own outcomes."""
    from .registry import ToolError  # local: registry imports tools, not the reverse

    def source(phone: str) -> Optional[List[Dict[str, Any]]]:
        try:
            return [to_record(item) for item in client.search(phone)]
        except WarrantyAPINoRecord:
            return None
        except WarrantyAPIError as exc:
            # The code the agents already know for "the warranty system is not
            # responding": the source changed, the outcome did not.
            raise ToolError(
                "oms_unavailable",
                "The warranty system is not responding (%s)." % type(exc).__name__,
                retryable=True,
            )

    return source
