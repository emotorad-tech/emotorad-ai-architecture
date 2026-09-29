import tempfile
import textwrap
import unittest
from pathlib import Path

from emotorad_ai.standard_responses import StandardResponseError, load_standard_responses

VALID = """\
id: std-thanks
status: approved
approved_by: Kushendra
criteria: The customer is only saying thank you.
replies:
  english: You're welcome.
  hinglish: Aapka swagat hai.
examples: [thanks]
counter_examples: [thanks but it is still broken]
"""


def write(directory, name, text):
    Path(directory, name).write_text(textwrap.dedent(text), encoding="utf-8")


class LoadTests(unittest.TestCase):
    def test_loads_an_approved_response(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, "a.yaml", VALID)
            [response] = load_standard_responses(Path(directory))
        self.assertTrue(response.approved)
        self.assertEqual(response.reply_for("english"), "You're welcome.")
        self.assertIsNone(response.reply_for("hindi"))
        self.assertEqual(response.examples, ("thanks",))

    def test_the_seed_drafts_load_and_none_is_approved(self):
        responses = load_standard_responses()
        self.assertEqual(
            sorted(r.id for r in responses),
            ["std-acknowledged", "std-are-you-a-bot", "std-thanks-goodbye"],
        )
        self.assertFalse(any(r.approved for r in responses))

    def test_invalid_responses_are_refused_at_load(self):
        bad = {
            "coverage claim": VALID.replace("You're welcome.", "Good news, it's covered under warranty."),
            "fault conclusion": VALID.replace("You're welcome.", "Your battery is dead, so we will arrange a replacement."),
            "placeholder": VALID.replace("You're welcome.", "You're welcome, {name}."),
            "approved without approver": VALID.replace("approved_by: Kushendra", "approved_by: ''"),
            "unknown status": VALID.replace("status: approved", "status: live"),
            "unknown language": VALID.replace("  hinglish:", "  french:"),
            "empty criteria": VALID.replace("criteria: The customer is only saying thank you.", "criteria: ''"),
            "no replies": VALID.split("replies:")[0] + "replies: {}\n",
        }
        for name, text in bad.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                write(directory, "a.yaml", text)
                with self.assertRaises(StandardResponseError):
                    load_standard_responses(Path(directory))

    def test_duplicate_ids_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write(directory, "a.yaml", VALID)
            write(directory, "b.yaml", VALID)
            with self.assertRaises(StandardResponseError):
                load_standard_responses(Path(directory))


if __name__ == "__main__":
    unittest.main()
