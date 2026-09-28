"""A named test rider for exercising the Amiigo channel end to end.

`sess-amiigo-test` is the session a tester types to talk to the bot as a
signed-in Amiigo user. It must resolve exactly as a real Amiigo session would:
verified, phone-keyed, and hydrated from the warranty table with bikes whose
every field is populated, so the bot has something real-looking to say about
each one.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import date

from emotorad_ai import cli
from emotorad_ai.adapters import AmiigoAdapter
from emotorad_ai.contract import VERIFIED
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry

SESSION = "sess-amiigo-test"


class AmiigoTestSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_registry(today=date(2026, 9, 28))
        self.resolver = IdentityResolver(self.registry)
        self.adapter = AmiigoAdapter(self.resolver)

    def test_the_session_resolves_to_a_verified_amiigo_customer(self) -> None:
        message = self.adapter.to_message({"session_token": SESSION, "text": "hi"})

        self.assertEqual(message.channel, "amiigo_app")
        self.assertEqual(message.persona, "customer")
        self.assertEqual(message.identity.strength, VERIFIED)
        self.assertEqual(message.identity.phone, fixtures.PHONE_AMIIGO_TEST_RIDER)

    def test_the_rider_is_hydrated_with_a_name_and_two_bikes(self) -> None:
        message = self.adapter.to_message({"session_token": SESSION, "text": "hi"})
        resolved = self.resolver.hydrate(message)

        self.assertEqual(resolved.method, "verified")
        self.assertEqual(resolved.profile["name"], "Kabir Sharma")
        self.assertEqual(len(resolved.bikes), 2)

    def test_one_bike_is_in_warranty_and_the_other_is_not(self) -> None:
        message = self.adapter.to_message({"session_token": SESSION, "text": "hi"})
        coverage = {
            bike["frame_number"]: bike["in_warranty"]
            for bike in self.resolver.hydrate(message).bikes
        }

        self.assertEqual(coverage, {"EMXP2026001234": True, "DDL32023045678": False})

    def test_every_field_of_every_record_is_populated(self) -> None:
        # Other fixtures deliberately carry blanks to mirror the real OMS. This
        # one exists to show the bot a complete record, so a blank is a bug.
        for record in fixtures.WARRANTY_RECORDS[fixtures.PHONE_AMIIGO_TEST_RIDER]:
            for field_name, value in record.items():
                with self.subTest(frame=record["frame_number"], field=field_name):
                    self.assertTrue(value, "%s is empty" % field_name)


class CliAmiigoChannelTests(unittest.TestCase):
    def test_cli_can_talk_as_the_amiigo_test_rider(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["--offline", "--channel", "amiigo", "--session", SESSION, "hi"])

        self.assertEqual(code, 0)
        self.assertIn("assistant:", out.getvalue())


class PlaygroundPresetTests(unittest.TestCase):
    def test_the_rider_is_a_playground_preset_on_the_amiigo_channel(self) -> None:
        from emotorad_ai.playground import CUSTOMER_SCENARIOS

        presets = [s for s in CUSTOMER_SCENARIOS if s.phone == fixtures.PHONE_AMIIGO_TEST_RIDER]

        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0].channel, "amiigo_app")
        self.assertEqual(presets[0].strength, VERIFIED)


if __name__ == "__main__":
    unittest.main()
