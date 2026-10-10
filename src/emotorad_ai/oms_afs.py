"""OMS after-sales orders over its WebSocket (spec 2026-10-10 replacement
orders, section 5).

`afs_order_add` exists only as a WebSocket action (em-biz-backend
order/afs_order.py), reached at `<ws_url><token>/` as the OMS admin user whose
email, password and token a person keeps in Secrets Manager. The stored token
is used first; when OMS refuses it, the client logs in once with the email and
password and keeps the new token in memory. One call opens one socket, sends
one frame, waits for the frame OMS echoes back with the same `url` and
`client_ref`, and closes. No setting value is ever logged or put in an error:
errors carry an HTTP status or an exception's class.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

SWITCH_ENV = "EMOTORAD_OMS_AFS_ORDERS"
REQUIRED = ("EMOTORAD_OMS_WS_URL", "EMOTORAD_OMS_LOGIN_URL", "EMOTORAD_OMS_ADMIN_EMAIL", "EMOTORAD_OMS_ADMIN_PASSWORD")
TOKEN_ENV = "EMOTORAD_OMS_ADMIN_TOKEN"
SETTING_NAMES = REQUIRED + (TOKEN_ENV,)
DEVICE_ID = "emotorad-ai-chatbot"
SALE_TYPE = "Warranty"
REMARK = "Warranty replacement placed by the EMotorad support chatbot."
AUTH_CODE = 403
OK_CODE = 200


class OMSAuthError(Exception):
    """OMS refused the token, or the login failed. The message is a status or a class name."""


class OMSCallError(Exception):
    """OMS could not be reached or did not answer in time. The message is a class name."""


class OMSRefused(Exception):
    """OMS answered with an error status. An order may still have been made."""

    def __init__(self, status: Any, msg: str) -> None:
        super().__init__("status %s" % status)
        self.status, self.msg = status, msg


@dataclass(frozen=True)
class AFSSettings:
    ws_url: str = field(repr=False)
    login_url: str = field(repr=False)
    email: str = field(repr=False)
    password: str = field(repr=False)
    token: Optional[str] = field(default=None, repr=False)


def settings_from_env(environ: Optional[Mapping[str, str]] = None) -> Tuple[Optional[AFSSettings], str]:
    """The settings when the switch is exactly `on` and every required one is
    set, with /health's text: "on", "off" or "misconfigured: <names>"."""
    env = os.environ if environ is None else environ
    if (env.get(SWITCH_ENV) or "").strip() != "on":
        return None, "off"
    missing = [name for name in REQUIRED if not (env.get(name) or "").strip()]
    if missing:
        return None, "misconfigured: " + ", ".join(missing)
    return AFSSettings(ws_url=env["EMOTORAD_OMS_WS_URL"].strip(), login_url=env["EMOTORAD_OMS_LOGIN_URL"].strip(),
                       email=env["EMOTORAD_OMS_ADMIN_EMAIL"].strip(), password=env["EMOTORAD_OMS_ADMIN_PASSWORD"],
                       token=(env.get(TOKEN_ENV) or "").strip() or None), "on"


def _ws_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)


def _http_post(url: str, json: Any = None, timeout: Optional[float] = None) -> Any:
    import httpx

    return httpx.post(url, json=json, timeout=timeout)


def afs_request(order: Dict[str, Any], pin_code_id: str, sale_type_id: str) -> Dict[str, Any]:
    """The `afs_order_add` request for one ledger order: a customer order (CO),
    free (rate 0), sale type Warranty, our reference in ticket_number."""
    customer = order.get("customer") or {}
    address = customer.get("address") or {}
    request: Dict[str, Any] = {"order_type": "CO"}
    for side in ("bill", "ship"):
        request.update({
            "%s_customer_name" % side: customer.get("name"),
            "%s_mobile" % side: customer.get("mobile"),
            "%s_email" % side: customer.get("email"),
            "%s_pin_code_id" % side: pin_code_id,
            "%s_address" % side: address.get("line1") or "",
            "%s_address2" % side: address.get("line2") or "",
        })
    request.update({
        "items": [{"product_id": order.get("product_id"), "product_qty": 1, "is_demo": False,
                   "rate": 0, "amount": 0, "idx": 1}],
        "sale_type": SALE_TYPE,
        "sale_type_id": sale_type_id,
        "frame_number": order.get("frame_number"),
        "ticket_number": order["_id"],
        "remark": REMARK,
    })
    return request


class AFSClient:
    def __init__(self, settings: AFSSettings, connect: Optional[Callable[..., Any]] = None,
                 post: Optional[Callable[..., Any]] = None, log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                 timeout: float = 20.0) -> None:
        self._settings = settings
        self._connect = connect or _ws_connect
        self._post = post or _http_post
        self._log = log
        self._timeout = timeout
        self._token: Optional[str] = settings.token
        self._sale_types: Dict[str, str] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "AFSClient(settings=set)"

    # -- the three calls -----------------------------------------------------

    def find(self, reference: str) -> Optional[Dict[str, str]]:
        """Our order in OMS, by the reference in its ticket_id, or None."""
        response = self._call("afs_order_list", {"search": reference, "limit": 5, "page_no": 1}, reference + ":find")
        if response.get("status") == OK_CODE:
            rows = (response.get("data") or {}).get("data") or []
            row = next((r for r in rows if r.get("ticket_id") == reference), None)
            return {"order_code": str(row.get("order_code")), "order_id": str(row.get("id"))} if row else None
        if "not found" in str(response.get("msg") or "").lower():
            return None
        raise OMSCallError("find status %s" % response.get("status"))

    def place(self, request: Dict[str, Any], client_ref: str) -> Dict[str, str]:
        response = self._call("afs_order_add", request, client_ref)
        if response.get("status") == OK_CODE:
            data = response.get("data") or {}
            return {"order_code": str(data.get("order_code")), "order_id": str(data.get("id"))}
        raise OMSRefused(response.get("status"), str(response.get("msg") or ""))

    def sale_type_id(self, name: str) -> str:
        with self._lock:
            if name in self._sale_types:
                return self._sale_types[name]
        response = self._call("sale_type_list", {"limit": 100, "page_no": 1}, "sale_type:" + name)
        rows = (response.get("data") or {}).get("data") or []
        found = next((r for r in rows if r.get("sale_type") == name), None)
        if response.get("status") != OK_CODE or found is None:
            raise OMSCallError("sale_type_missing")
        with self._lock:
            self._sale_types[name] = str(found["id"])
            return self._sale_types[name]

    # -- the socket ------------------------------------------------------------

    def _call(self, url: str, request: Dict[str, Any], client_ref: str) -> Dict[str, Any]:
        token = self._token or self._login()
        try:
            return self._once(token, url, request, client_ref)
        except OMSAuthError:
            return self._once(self._login(), url, request, client_ref)

    def _once(self, token: str, url: str, request: Dict[str, Any], client_ref: str) -> Dict[str, Any]:
        frame = {"transmit": "single", "url": url, "client_ref": client_ref, "request": request}
        deadline = time.monotonic() + self._timeout
        try:
            socket = self._connect("%s%s/" % (self._settings.ws_url, token), open_timeout=self._timeout,
                                   close_timeout=2)
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == AUTH_CODE:
                raise OMSAuthError("connect 403") from None
            raise OMSCallError(type(exc).__name__) from None
        with socket:
            try:
                socket.send(json.dumps(frame))
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise OMSCallError("timeout")
                    answer = json.loads(socket.recv(timeout=left))
                    if answer.get("url") == "unauthorized":
                        raise OMSAuthError("unauthorized")
                    if answer.get("url") == url and answer.get("client_ref") == client_ref:
                        response = answer.get("response") or {}
                        if response.get("status") == AUTH_CODE:
                            raise OMSAuthError("status 403")
                        return response
            except (OMSAuthError, OMSCallError):
                raise
            except Exception as exc:
                raise OMSCallError(type(exc).__name__) from None

    def _login(self) -> str:
        try:
            answer = self._post(self._settings.login_url, json={
                "email": self._settings.email, "password": self._settings.password,
                "device_type": "web", "device_id": DEVICE_ID}, timeout=10)
        except Exception as exc:
            if self._log is not None:
                self._log("oms_login_failed", {"error": type(exc).__name__})
            raise OMSAuthError("login %s" % type(exc).__name__) from None
        token = None
        if answer.status_code == 200:
            try:
                token = ((answer.json() or {}).get("data") or {}).get("access_token")
            except ValueError:
                token = None
        if not token:
            if self._log is not None:
                self._log("oms_login_failed", {"status": answer.status_code})
            raise OMSAuthError("login %s" % answer.status_code)
        with self._lock:
            self._token = token
        return token
