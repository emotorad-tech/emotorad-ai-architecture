"""What a migrated flow must still contain.

Moving a procedure from the prompt into a record is the change most likely to
lose something quietly: retrieval keeps passing, the record is found, and a rule
that used to be in the prompt simply is not in the answer any more. Nothing else
in the suite would notice.

So each migrated flow is pinned to the rules it carried. These are not style
checks — every entry below is a rule that was written into the prompt because a
real conversation went wrong without it, and the phrase searched for is the one
that carries it.

Rewording a record is fine; the assertion is on the *rule*, and if a rewrite
drops one, this fails and asks whether that was deliberate.
"""

import unittest

from emotorad_ai.knowledge import load_records


def _body(record_id):
    record = next(r for r in load_records() if r.id == record_id)
    return (" ".join(record.steps) + " " + record.escalate_when).lower()


class StandardBatteryFlowTests(unittest.TestCase):
    """§5c — every model except Doodle."""

    RULES = {
        "SOC check comes first": "soc button",
        "press and hold 1-2 seconds": "one to two seconds",
        "90+ days raises sleep-mode likelihood": "90+ days",
        "no-light means charger/socket, not battery": "socket has power",
        "offer the borrowed-charger test": "borrow",
        "red means drained: charge four hours": "at least four hours",
        "first LED shows red about ten minutes in": "ten minutes",
        "green routes to the revival process": "revival process",
        "revival is eight to ten presses": "eight to ten",
        "the branch is locked once the LED is read": "stay in it",
        "revival belongs to the green row only": "green row and to no other",
        "never borrow a neighbouring procedure": "borrow the nearest procedure",
        "under four hours is normal, not a fault": "less than four hours",
        "post-charge check needs the full four hours": "post-charge check",
        "no dead conclusion without the video": "only once you have that video",
    }

    def test_every_rule_survived_the_move(self):
        body = _body("battery-wont-power-on")
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost in migration: %s" % missing)


class DoodleBatteryFlowTests(unittest.TestCase):
    """§5d — Doodle V1 through V4 and Pro."""

    RULES = {
        "no SOC button and no on/off switch": "no soc button",
        "red means the opposite of red elsewhere": "opposite of red",
        "no light points at the charger": "charger is the likely fault",
        "borrowed-charger test": "borrow",
        "green is ambiguous": "either dead or simply fully charged",
        "the 60-day question decides the branch": "60 days",
        "ask the date question first": "answer the first question before the second",
        "instant green on a recent charge proves nothing": "not evidence of a dead pack",
        "multimeter before blaming another part": "multimeter",
        "no meter means say you do not know": "you do not know",
        "never clear the battery on the 60-day answer alone": "60-day answer alone",
    }

    def test_every_rule_survived_the_move(self):
        body = _body("battery-doodle-wont-power-on")
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost in migration: %s" % missing)


class SocIndicatorFlowTests(unittest.TestCase):
    """§5e — the pack works, only the indicator is dead."""

    RULES = {
        "distinguish from won't-power-on": "not the won't-power-on flow",
        "ask whether it ever worked": "never lit",
        "a short tap does nothing": "short tap",
        "one continuous take, not four clips": "single continuous take",
        "the take shows the bike running": "does it run",
        "under a year means replacement": "under a year",
        "a year or more means service centre": "a year or more",
        "say which date you are using": "registration date",
        "never promise a replacement on an inferred date": "date you inferred",
    }

    def test_every_rule_survived_the_move(self):
        body = _body("battery-soc-indicator-dead")
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost in migration: %s" % missing)


class OnOffSwitchFlowTests(unittest.TestCase):
    """§5f — the switch is dead, so nothing powers on."""

    RULES = {
        "ask how long": "how long",
        "photos from every side": "every side",
        "video of the switch being operated": "actually being operated",
        "no all-sides reference exists yet": "no all-sides reference",
        "three asks then a dealer": "three asks",
        "frame number must match the record": "frame number",
        "no fault visible is a real outcome": "cannot see anything wrong",
        "report a damaged sticker, do not rule on it": "looks damaged in the photo",
        "never state the warranty is void": "never tell a customer their warranty is void",
    }

    def test_every_rule_survived_the_move(self):
        body = _body("battery-onoff-switch-dead")
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost in migration: %s" % missing)


class MigratedFlowsAreScopedTests(unittest.TestCase):
    """A migrated flow must reach its own bikes and no others."""

    def test_the_two_power_on_flows_never_both_apply(self):
        records = {r.id: r for r in load_records()}
        standard = records["battery-wont-power-on"]
        doodle = records["battery-doodle-wont-power-on"]
        self.assertEqual(doodle.applies_to.get("product_name"), "doodle")
        self.assertEqual(standard.excludes.get("product_name"), "doodle")


if __name__ == "__main__":
    unittest.main()


class MeltingFlowTests(unittest.TestCase):
    """§5b1, which was reachable two ways and had to stay reachable both ways.

    The rule that matters most here is the second photo. It was written into the
    prompt because customers were being closed out on a clean terminal photo and
    coming back a week later with a melted controller connector; a record that
    drops it regresses to exactly that.
    """

    RECORD_ID = "battery-melted-terminal"
    RULES = {
        "the second photo is asked for regardless": "whatever the first shows",
        "and again, explicitly, at the controller step": "even when the terminal turned",
        "the reason is said out loud": "how far the heat travelled",
        "melting is a heat event, not wiring": "heat event, not a wiring fault",
        "the bike is taken out of use": "not to keep using or charging",
        "no diagnosis from a photo that cannot be read": "too dark or too blurred",
        "no fault found is a real outcome": "rather than inventing one",
        "E-06 with both ends clean lands on the BMS": "battery management system",
    }

    def test_every_rule_survived_the_move(self):
        body = _body(self.RECORD_ID)
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost in migration: %s" % missing)

    def test_reachable_from_the_error_code_that_sends_customers_here(self):
        """E-06's `verification` describes this check; the record must answer to it.

        The two live in different files and neither imports the other, so nothing
        but this test notices if the code table starts pointing at a flow that no
        longer exists.
        """
        from emotorad_ai.errorcodes import load_table

        entry = load_table().lookup("E-06", "X2 Furious Red V2")["entry"]
        self.assertIn("melted", entry["verification"].lower())
        from emotorad_ai.knowledge import KnowledgeBase

        found = KnowledgeBase(load_records()).search(
            "melted charging port E-06", topic="battery", bike={"throttle": "yes"}
        )
        self.assertIn(self.RECORD_ID, [p.id for p in found])

