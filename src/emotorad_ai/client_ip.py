"""The customer's own IP address, behind nginx.

nginx sets `X-Real-IP $remote_addr`, overwriting anything the client sent.
The container sees every request from Docker's bridge gateway, so the header
is the only place the customer's address is. It is trusted only when the
request comes straight from a trusted proxy; from anyone else it is ignored,
so a client cannot choose its own address (its rate-limit bucket, or the
place recorded for its conversation).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any, Mapping, Optional

TRUSTED_PROXIES_ENV = "EMOTORAD_TRUSTED_PROXIES"
DEFAULT_TRUSTED_PROXIES = "127.0.0.1,::1,172.17.0.1"


def trusted_from_env(environ: Optional[Mapping[str, str]] = None) -> frozenset:
    env = environ if environ is not None else os.environ
    raw = env.get(TRUSTED_PROXIES_ENV) or DEFAULT_TRUSTED_PROXIES
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def client_ip(request: Any, trusted: frozenset) -> Optional[str]:
    peer = request.client.host if request.client else None
    if peer in trusted:
        forwarded = (request.headers.get("x-real-ip") or "").strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer
