"""Test package bootstrap: put `src/` on the path so tests import the package
without an install step (`python3 -m unittest discover -s tests -t .`).
"""

import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
