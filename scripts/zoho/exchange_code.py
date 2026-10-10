"""Swap a Zoho grant code for the chatbot's refresh token (spec 2026-10-05, section 10, person step 4).

    python scripts/zoho/exchange_code.py --redirect-uri <the same address as consent_url.py>
    python scripts/zoho/exchange_code.py --self-client

Not part of the setup while the chatbot shares the OMS's Zoho token (Sachin's
decision, 5 October 2026): kept for a future client of our own.

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

The redirect address is trimmed of spaces and printed before the exchange.
A network failure or a server error is not a refusal: it names the error and
says to run the script again.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import (
    REDIRECT_RULE, TOKEN_URL, ask_secret, exchange_form, granted_scopes, post_form, redirect_address, revoke,
    says_india,
)
from emotorad_ai.zoho.errors import ZohoError
from emotorad_ai.zoho.http import DeskHTTP

WARNING = (
    "The refresh token is on the next line, shown once. Put it straight into the config store "
    "(docs/runbooks/config-store.md, section 7) or a password manager. Never paste it into a chat, "
    "a file in a repo or a ticket. Then clear this terminal's scrollback."
)


# The code was neither taken nor turned down, as far as the script can tell.
TRY_AGAIN = ("Nothing says the code is bad: run exchange_code.py again. A code lasts two minutes and works once, "
             "so if they have passed, or Zoho then says invalid_code, approve again first and use the new code.")


def refused(error: str, redirect: Optional[str]) -> str:
    """What the person is told when Zoho turns the code down."""
    text = "Zoho refused the code (error=%s). Codes last two minutes: approve again and rerun." % error
    if redirect:
        text += " Redirect address: %s. %s It must be the one given to consent_url.py too." % (redirect, REDIRECT_RULE)
    return text


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    kind = p.add_mutually_exclusive_group(required=True)
    kind.add_argument("--redirect-uri", help="a server-based client: the address used in consent_url.py")
    kind.add_argument("--self-client", action="store_true", help="a Self Client: no redirect address")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass, http: Any = None,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    redirect = None
    if not args.self_client:
        redirect = redirect_address(args.redirect_uri)
        if redirect is None:
            out("--redirect-uri is empty. %s Nothing was sent." % REDIRECT_RULE)
            return 1
        out("Redirect address: %s" % redirect)
    client_id = ask_secret("Zoho client id: ", ask)
    client_secret = ask_secret("Zoho client secret: ", ask)
    code = ask_secret("Grant code from the address bar: ", ask)
    http = http if http is not None else DeskHTTP()
    form = exchange_form(client_id, client_secret, code, redirect)
    try:
        status, answer = post_form(http, TOKEN_URL, form)
    except ZohoError as exc:
        out("Zoho could not be reached or did not answer (%s, error=%s). %s" % (type(exc).__name__, exc.error,
                                                                               TRY_AGAIN))
        return 1
    if status >= 500:
        out("Zoho answered %d, a server error (error=%s). %s" % (status, answer.get("error") or "http_%d" % status,
                                                                 TRY_AGAIN))
        return 1
    if answer.get("error") or status >= 400:
        out(refused(str(answer.get("error") or "http_%d" % status), redirect))
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
