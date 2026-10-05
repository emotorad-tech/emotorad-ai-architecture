"""Revoke the chatbot's Zoho refresh token (spec 2026-10-05, section 10, rollback).

    python scripts/zoho/revoke.py

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards.

It asks for the refresh token by hidden input, and posts it to
https://accounts.zoho.in/oauth/v2/revoke/token as a form field, never in the
address, as Zoho's revoke page documents. Only that token stops working.
Revoking through Zoho's Connected Apps page works per app, and could revoke the
OMS's token too, so do not use it. Never give this script the OMS's token.
Check Zoho's current revoke page before relying on it: REVOKE_URL in _common.py
is the one place to change the address.

After revoking, remove EMOTORAD_ZOHO_REFRESH_TOKEN from the config store and
redeploy (docs/runbooks/config-store.md, section 7). Until then /health says
"token refused".
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any, Callable, List, Optional

from _common import ask_secret, revoke
from emotorad_ai.zoho.http import DeskHTTP


def parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__.splitlines()[0])


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         typed: Callable[[str], str] = input, http: Any = None, out: Callable[[str], None] = print) -> int:
    parser().parse_args(argv)
    token = ask_secret("The chatbot's refresh token to revoke: ", ask)
    if typed("Type REVOKE to revoke it. This cannot be undone: ").strip() != "REVOKE":
        out("Nothing revoked.")
        return 1
    done, said = revoke(http if http is not None else DeskHTTP(), token)
    out("Revoked. The chatbot can no longer reach Zoho with that token." if done else "Not revoked (error=%s)." % said)
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
