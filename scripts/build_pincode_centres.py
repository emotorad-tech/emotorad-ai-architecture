#!/usr/bin/env python3
"""Build src/emotorad_ai/data/pincode_centres.csv (spec 2026-10-09, section 1).

The source is India Post's "All India Pincode Directory" (data.gov.in, Open
Government Data Licence India), from the public mirror our pincodes.csv came
from. Download it once, then run:

    curl -sSo /tmp/all-india-pincodes.csv \
      https://raw.githubusercontent.com/arobindo/pincode-india-csv/main/all-india-pincodes.csv
    python scripts/build_pincode_centres.py /tmp/all-india-pincodes.csv
"""

import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from emotorad_ai.address import PincodeDirectory  # noqa: E402
from emotorad_ai.geo import CENTRES_PATH, HEADER, build_centres  # noqa: E402

SOURCE = ("https://raw.githubusercontent.com/arobindo/pincode-india-csv/main/all-india-pincodes.csv")


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    offices = []
    with open(argv[1], newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            offices.append((row["pincode"], _number(row.get("latitude")), _number(row.get("longitude"))))
    directory = PincodeDirectory.load()
    district_of = {}
    for pincode, _lat, _lon in offices:
        places = directory.lookup(str(pincode).strip())
        if places:
            district_of[str(pincode).strip()] = "%s|%s" % (places[0].district, places[0].state)
    centres = build_centres(offices, district_of)
    CENTRES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CENTRES_PATH.open("w", newline="") as out:
        out.write("# India Post pincode centres. Source: data.gov.in \"All India Pincode Directory\" "
                  "(Open Government Data Licence India), via %s, built %s by "
                  "scripts/build_pincode_centres.py. Do not hand-edit.\n" % (SOURCE, date.today().isoformat()))
        writer = csv.writer(out)
        writer.writerow(HEADER)
        for pincode in sorted(centres):
            lat, lon, basis = centres[pincode]
            writer.writerow([pincode, "%.4f" % lat, "%.4f" % lon, basis])
    by_basis = {}
    for _lat, _lon, basis in centres.values():
        by_basis[basis] = by_basis.get(basis, 0) + 1
    print("wrote %d pincodes to %s: %s" % (len(centres), CENTRES_PATH, by_basis))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
