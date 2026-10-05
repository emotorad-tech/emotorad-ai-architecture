"""Digits of any script read as ASCII (digits.py, spec 2026-10-05, section 8).

Moved out of verify_first.py so observability.py can use it: verify_first
imports observability, so the other way round would be an import cycle.
"""

import ast
import unittest
from pathlib import Path

from emotorad_ai import digits, verify_first
from emotorad_ai.digits import ascii_digits


class AsciiDigitsTests(unittest.TestCase):
    def test_devanagari_digits_become_ascii(self):
        self.assertEqual(ascii_digits("९८७६५४३२१०"), "9876543210")

    def test_digits_of_other_scripts_too(self):
        # Bengali and Tamil, as a keyboard in either script types them.
        self.assertEqual(ascii_digits("৯৮৭"), "987")
        self.assertEqual(ascii_digits("௯௮௭"), "987")

    def test_everything_else_is_left_as_typed(self):
        text = "मेरा नंबर 98765 है, battery ½ charged²"
        self.assertEqual(ascii_digits(text), text)

    def test_verify_first_still_offers_the_same_function(self):
        self.assertIs(verify_first.ascii_digits, ascii_digits)

    def test_the_module_imports_nothing_from_the_package(self):
        tree = ast.parse(Path(digits.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "digits.py must not import from the package")
                self.assertFalse((node.module or "").startswith("emotorad_ai"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("emotorad_ai"))


if __name__ == "__main__":
    unittest.main()
