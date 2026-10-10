"""Print Zoho's consent address for the chatbot's grant (spec 2026-10-05, section 10, person step 4).

    python scripts/zoho/consent_url.py --redirect-uri <the redirect address registered on the client>

Run by a person, never by a Claude session, in a terminal window outside the
Claude app (its Terminal panel can be read by the session). Clear the
scrollback afterwards: the address holds the client id.

For a server-based client only (person step 2 decides). It asks for the client
id by hidden input and prints the India consent address: response_type=code,
the client id, every scope the chatbot needs (comma-separated, SCOPES in
_common.py), the redirect address, access_type=offline and prompt=consent.
Open it signed in to Zoho as the granting user, approve, and copy the code
from the address bar (the page itself may show an error). Then run
exchange_code.py within two minutes. The redirect address is printed beside
the link, trimmed of spaces: Zoho refuses one that differs by a character
from the address registered on the client ("Invalid Redirect Uri").
"""

from __future__ import annotations

import argparse
import getpass
from typing import Callable, List, Optional

from _common import REDIRECT_RULE, ask_secret, consent_url, redirect_address


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--redirect-uri", required=True, help="the redirect address registered on the OMS's client")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    redirect = redirect_address(args.redirect_uri)
    if redirect is None:
        out("--redirect-uri is empty. %s Nothing was printed." % REDIRECT_RULE)
        return 1
    client_id = ask_secret("Zoho client id: ", ask)
    out("Open this address while signed in to Zoho as the granting user, then approve:")
    out("Redirect address: %s" % redirect)
    out(consent_url(client_id, redirect))
    out('If Zoho says "Invalid Redirect Uri", the redirect address above is not the one registered on the '
        "client, character for character. Copy it from the client's settings and rerun.")
    out("Copy the code= value from the address bar (the page may show an error), "
        "then run exchange_code.py within two minutes, with the same redirect address.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
