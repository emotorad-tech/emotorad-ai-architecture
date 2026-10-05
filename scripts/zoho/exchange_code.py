"""Swap a Zoho grant code for the chatbot's refresh token (spec 2026-10-05, section 10, person step 4).

    python scripts/zoho/exchange_code.py --redirect-uri <the same address as consent_url.py>
    python scripts/zoho/exchange_code.py --self-client

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session), within two minutes
of approving. Clear the scrollback afterwards.

It asks for the client id, the client secret and the code by hidden input, and
posts them to https://accounts.zoho.in/oauth/v2/token: with the redirect
address for a server-based client, without it for a Self Client (spec section
1: a Self Client stops part 1 until Sachin decides). It refuses unless Zoho's
answer names the India data centre. A token issued anywhere else is revoked at
once and never shown. Otherwise it prints the refresh token once, with a
warning. Keep it somewhere safe until it goes into the config store (person
step 8). Replaces reports/zoho-probe/exchange_code.py.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import TOKEN_URL, ask_secret, exchange_form, granted_scopes, post_form, revoke, says_india
from emotorad_ai.zoho.errors import ZohoError
from emotorad_ai.zoho.http import DeskHTTP

WARNING = (
    "The refresh token is on the next line, shown once. Put it straight into the config store "
    "(docs/runbooks/config-store.md, section 7) or a password manager. Never paste it into a chat, "
    "a file in a repo or a ticket. Then clear this terminal's scrollback."
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--redirect-uri", help="a server-based client: the address used in consent_url.py")
    kind.add_argument("--self-client", action="store_true", help="a Self Client: no redirect address")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass, http: Any = None,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    client_secret = ask_secret("Zoho client secret: ", ask)
    code = ask_secret("Grant code from the address bar: ", ask)
    http = http if http is not None else DeskHTTP()
    form = exchange_form(client_id, client_secret, code, None if args.self_client else args.redirect_uri)
    try:
        _, answer = post_form(http, TOKEN_URL, form)
    except ZohoError as exc:
        out("Zoho refused the code (error=%s). Codes last two minutes: approve again and rerun." % exc.error)
        return 1
    if answer.get("error"):
        out("Zoho refused the code (error=%s). Codes last two minutes: approve again and rerun." % answer["error"])
        return 1
    refresh_token = answer.get("refresh_token")
    if not says_india(answer):
        out("Zoho's answer does not name the India data centre, so the token is not shown.")
        if refresh_token:
            done, said = revoke(http, refresh_token)
            out("The token Zoho issued was revoked." if done else
                "The token Zoho issued was NOT revoked (error=%s). Ask the Zoho admin to remove it." % said)
        return 1
    if not refresh_token:
        out("Zoho gave no refresh token. The consent address needs access_type=offline and prompt=consent "
            "(consent_url.py adds both). Approve again and rerun.")
        return 1
    granted = granted_scopes(answer)
    out("Scopes granted: %s" % (", ".join(granted) if granted else "Zoho did not say"))
    out(WARNING)
    out(refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
