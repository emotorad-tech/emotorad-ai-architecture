"""The prompt the service runs is the promoted one, and it is reviewable.

There were two prompts and only one of them ran. The playground tuned a base
prompt up to v28, 22,162 characters carrying every rule added over a fortnight,
and `.playground/prompts/battery_support.json` held all 28 versions. The service
ran the 2,641-character literal in `agents/battery_support.py`, which had none
of them: no coverage matrix, no search-first rule, no ticket consent.

Neither file was wrong. The step between them was a person copying text into the
module, it had not been run since v24, and nothing anywhere said so. These tests
are the "nothing said so" part.
"""

import unittest
from pathlib import Path

from emotorad_ai.agents import battery_support
from emotorad_ai.prompts import load_base_prompt, prompt_path


class ActivePromptTests(unittest.TestCase):
    AGENT = "battery_support"

    def test_the_agent_runs_the_promoted_file_not_its_own_literal(self):
        promoted = load_base_prompt(self.AGENT)
        self.assertIsNotNone(
            promoted,
            "prompts/%s.md is missing. Run scripts/promote_prompt.py %s"
            % (self.AGENT, self.AGENT),
        )
        self.assertEqual(battery_support._BASE_PROMPT, promoted)
        self.assertIsNot(
            battery_support._BASE_PROMPT,
            battery_support._FALLBACK_PROMPT,
            "the agent fell back to its in-code prompt, so the tuned one is not running",
        )

    def test_the_promoted_prompt_is_the_substantial_one(self):
        # Not a style check. The in-code fallback is a short generic brief; the
        # promoted prompt carries the flows, gates and coverage rules. If the
        # active prompt is anywhere near the fallback's size, a promotion has
        # been reverted or the file has been truncated.
        self.assertGreater(
            len(battery_support._BASE_PROMPT),
            len(battery_support._FALLBACK_PROMPT) * 3,
        )

    def test_rules_that_only_exist_in_the_tuned_prompt_are_live(self):
        # One marker per rule that was written after the in-code prompt stopped
        # being updated. Each was added because a real transcript went wrong
        # without it, and each was invisible to the running service until now.
        active = battery_support._BASE_PROMPT
        for marker, why in (
            ("5g. Who pays", "coverage and cause decide cost"),
            ("Never describe your own machinery", "no narrating tools at the customer"),
            ("symptom first, then search", "search before asking"),
            ("Consent before a ticket", "two-turn ticket consent"),
            ("One question per message", "no bulleted interrogation"),
        ):
            self.assertIn(marker, active, why)
            self.assertNotIn(marker, battery_support._FALLBACK_PROMPT, why)

    def test_the_fallback_still_works_when_nothing_is_promoted(self):
        # A fresh clone with no prompts/ directory must still start. An agent
        # that has never been promoted is a normal state, not a broken deploy.
        self.assertIsNone(load_base_prompt(self.AGENT, directory=Path("/nonexistent")))
        self.assertTrue(battery_support._FALLBACK_PROMPT.strip())

    def test_an_empty_file_is_treated_as_no_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ("%s.md" % self.AGENT)).write_text("   \n\n", encoding="utf-8")
            self.assertIsNone(load_base_prompt(self.AGENT, directory=Path(tmp)))

    def test_the_live_prompt_is_tracked_where_a_reviewer_will_see_it(self):
        # The point of promoting into prompts/ rather than reading the
        # playground store directly: the live prompt changes in a pull request,
        # not the moment somebody clicks Save in a sandbox.
        path = prompt_path(self.AGENT)
        self.assertTrue(path.is_file())
        self.assertNotIn(".playground", str(path))


class PromptAssemblyTests(unittest.TestCase):
    """Promoting replaces the base text only. The live context still wraps it."""

    def _message(self):
        from emotorad_ai.contract import Identity, InboundMessage
        from emotorad_ai.identity import ResolvedIdentity

        identity = Identity(strength="verified", phone="9999999999")
        return (
            InboundMessage(
                conversation_id="c1",
                persona="customer",
                identity=identity,
                channel="website_chat",
                message_text="battery not working",
            ),
            ResolvedIdentity(persona="customer", method="phone", identity=identity),
        )

    def test_the_context_blocks_still_run_after_promotion(self):
        message, resolved = self._message()
        assembled = battery_support.DEFINITION.build_system_prompt(message, resolved, "")
        self.assertIn(battery_support._BASE_PROMPT, assembled)
        self.assertGreater(
            len(assembled),
            len(battery_support._BASE_PROMPT),
            "facts/context/entry blocks were dropped",
        )

    def test_the_playground_can_still_substitute_and_restores_afterwards(self):
        # `_tuned_system_prompt` monkey-patches the module global for one call.
        # Loading from a file at import must not break that, or the playground
        # silently starts testing the promoted prompt instead of the edit.
        from emotorad_ai.playground import _tuned_system_prompt

        message, resolved = self._message()
        before = battery_support._BASE_PROMPT
        tuned = _tuned_system_prompt(battery_support, message, resolved, "SANDBOX TEXT")
        self.assertTrue(tuned.startswith("SANDBOX TEXT"))
        self.assertIs(battery_support._BASE_PROMPT, before)


if __name__ == "__main__":
    unittest.main()
