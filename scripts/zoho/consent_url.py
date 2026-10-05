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
exchange_code.py within two minutes.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Callable, List, Optional

from _common import ask_secret, consent_url


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--redirect-uri", required=True, help="the redirect address registered on the OMS's client")
    return p


def main(argv: Optional[List[str]] = None, ask: Callable[[str], str] = getpass.getpass,
         out: Callable[[str], None] = print) -> int:
    args = parser().parse_args(argv)
    client_id = ask_secret("Zoho client id: ", ask)
    out("Open this address while signed in to Zoho as the granting user, then approve:")
    out(consent_url(client_id, args.redirect_uri))
    out("Copy the code= value from the address bar (the page may show an error), "
        "then run exchange_code.py within two minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
