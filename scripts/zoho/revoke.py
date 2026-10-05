"""Revoke a Zoho refresh token of a client of our own, never the OMS's (spec 2026-10-05, section 10).

    python scripts/zoho/revoke.py                 # refuses, and says why
    python scripts/zoho/revoke.py --own-client    # a future client of our own only

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

The chatbot shares the OMS's Zoho client and refresh token (Sachin's decision,
5 October 2026). Revoking that token stops the OMS's ticketing and AFS
dispatch, so never revoke it: not with this script and not through Zoho's
Connected Apps page. Without --own-client the script says so and stops before
it asks for anything. To stop the chatbot sending, remove
EMOTORAD_ZOHO_REFRESH_TOKEN from the AI config store and redeploy
(docs/runbooks/config-store.md, section 7). The OMS keeps its token.

With --own-client, for a token issued to a client of our own (consent_url.py
and exchange_code.py), it asks for the refresh token by hidden input and for
REVOKE typed out, and posts the token to
https://accounts.zoho.in/oauth/v2/revoke/token as a form field, never in the
address, as Zoho's revoke page documents. Only that token stops working.
Check Zoho's current revoke page before relying on it: REVOKE_URL in
_common.py is the one place to change the address. Then remove the token from
the config store and redeploy; until then /health says "token refused".
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import ask_secret, revoke
from emotorad_ai.zoho.http import DeskHTTP


# Said, and nothing else done, without --own-client.
SHARED = (
    "Not run. The chatbot shares the OMS's Zoho refresh token, so revoking it stops the OMS's ticketing "
    "and AFS dispatch. To stop the chatbot sending, remove EMOTORAD_ZOHO_REFRESH_TOKEN from the AI config "
    "store and redeploy (docs/runbooks/config-store.md, section 7). Never revoke the shared token, here or "
    "on Zoho's Connected Apps page. --own-client is only for a token of a client of our own."
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--own-client", action="store_true",
                   help="the token belongs to a client of our own, never the OMS's shared one")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         typed: Callable[[str], str] = input, http: Any = None, out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    if not args.own_client:
        out(SHARED)
        return 1
    token = ask_secret("The refresh token of our own client to revoke: ", ask)
    if typed("Type REVOKE to revoke it. This cannot be undone: ").strip() != "REVOKE":
        out("Nothing revoked.")
        return 1
    done, said = revoke(http if http is not None else DeskHTTP(), token)
    out("Revoked. The chatbot can no longer reach Zoho with that token." if done else "Not revoked (error=%s)." % said)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
