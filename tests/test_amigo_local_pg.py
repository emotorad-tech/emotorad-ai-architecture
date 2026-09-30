"""Runs scripts/amigo_staging/local_check.sh when EMOTORAD_TEST_LOCAL_PG=1:
the reader's real SQL against a local Postgres with the Amigo tables."""

import os
import pathlib
import shutil
import subprocess
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "amigo_staging" / "local_check.sh"


@unittest.skipUnless(os.environ.get("EMOTORAD_TEST_LOCAL_PG") == "1", "needs a local Postgres and the backend repo")
class LocalPostgresTests(unittest.TestCase):
    def test_the_reader_against_the_real_tables(self):
        # The bash on PATH (Git's on Windows): a bare "bash" finds System32's
        # WSL one first, which has no /c/ drive.
        bash = shutil.which("bash") or "bash"
        out = subprocess.run([bash, str(SCRIPT)], capture_output=True, text=True, timeout=300).stdout
        self.assertIn("A bikes: ['EMXPLUS', 'DOODLEPRO']", out)
        self.assertIn("B frame: 860000000000032 imei: 860000000000032", out)
        self.assertIn("{'stage': '1000 km / 6 months', 'status': 'due'}", out)
        self.assertIn("C trips: [12.6, 11.9, 12.4]", out)


if __name__ == "__main__":
    unittest.main()
