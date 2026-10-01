"""Fetch this month's DB-IP Lite city file into the image (origin.py).

Run at build time. The new file appears early in the month, so last month's
is the fallback. It never fails the build: without the file, /health says
"ip_location": "not configured" and conversations record the phone's country
or "unknown". DB-IP Lite is CC BY 4.0: "IP Geolocation by DB-IP".
"""

import gzip
import os
import shutil
import sys
import urllib.request
from datetime import date
from typing import Any, Callable, List, Optional

URL = "https://download.db-ip.com/free/dbip-city-lite-%s.mmdb.gz"
NAME = "dbip-city-lite.mmdb"


def months(today: date) -> List[str]:
    previous = date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)
    return [today.strftime("%Y-%m"), previous.strftime("%Y-%m")]


def fetch(dest_dir: str, today: date, opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[str]:
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, NAME)
    for month in months(today):
        partial = target + ".part"
        try:
            with opener(URL % month, timeout=300) as response, gzip.GzipFile(fileobj=response) as unpacked, \
                    open(partial, "wb") as out:
                shutil.copyfileobj(unpacked, out)
            os.replace(partial, target)
            print("geo db: dbip-city-lite-%s" % month)
            return month
        except Exception as exc:
            print("geo db: %s not fetched (%s)" % (month, type(exc).__name__))
            if os.path.exists(partial):
                os.remove(partial)
    return None


if __name__ == "__main__":
    fetch(sys.argv[1] if len(sys.argv) > 1 else "/app/geo", date.today())
