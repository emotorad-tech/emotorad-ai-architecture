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
        # Labels, because the labels were the bug. These steps are a branch and
        # were numbered STEP 1 / STEP 2 like a running order. A bike whose SOC
        # light came on healthy was sent to the charger check anyway: the model
        # had just done "STEP 1", saw "STEP 2" next, and did it — the numbering
        # asserted a sequence and outranked the condition written above it. Each
        # step now names the branch it belongs to, and none of them implies an
        # order the flow does not have.
        "the SOC answer is stated to pick exactly one branch": "picks one branch and only one",
        "the healthy branch must not read the charger steps": "do not read the charger steps",
        "the charger check names its precondition in its own label": "only if the soc showed no light",
        "and refuses to run on the healthy branch": "do not run this when the soc lit",
        # The same defect one level down: GREEN stated an unconditional meaning
        # that is only true on the no-light branch, so the record as written let
        # a customer with a confirmed-healthy pack be told it may be dead.
        "green is qualified by the branch it is read on": "on this branch",
        "green alone never carries the meaning": "never the charger led alone",
        # A pack switched off reads exactly like a dead one, so the switch is
        # confirmed before the reading that everything below depends on.
        # The other half of the same collision: this record claims the shared
        # phrasing rather than leaving scoring to arbitrate it.
        "a dark SOC on a dead bike is claimed by this record": "belongs to this record",
        "and is not surrendered on wording": "do not leave for",
        "the switch is ON before the SOC press, not optionally": "not optional",
        "why it matters is stated": "reads exactly like a dead one",
        # "Plug the charger in" was heard as any one of three things, and a
        # charger on a dead socket shows no light for reasons that are not the
        # battery's — which the flow would have read as a charger fault.
        "all three parts of the charger setup are spelled out": "all three parts of the charger setup",
        "the socket must be switched on at the wall": "switched on at the wall",
        # Revival was announced as the last check and then followed by two more.
        "revival is not billed as the last check": "do not call this the last check",
        # And the end of the flow was a destination that did not exist, so the
        # model hedged into "a deeper fault" and asked permission to act.
        "a failed revival with clean terminals is called dead, in those words": "the battery is dead. say so plainly",
        "hedging is named and refused": "not \"a deeper fault\"",
        "replacement happens in the same message, not on request": "go straight to replacement",
        "90+ days raises sleep-mode likelihood": "90+ days",
        "no-light means charger/socket, not battery": "socket has power",
        "offer the borrowed-charger test": "borrow",
        "red means drained: charge four hours": "at least four hours",
        "first LED shows red about ten minutes in": "ten minutes",
        "green routes to the revival process": "revival process",
        "revival is eight to ten presses": "eight to ten",
        # Both directions of the 60-day question, because only one of them was
        # written down the first time. "60+ days means sleep" was stated and the
        # recent-charge branch said only that sleep was "less likely" — naming
        # what the answer is not evidence for and never what it is. Deep sleep
        # was then the only diagnosis in the step, so a customer who had charged
        # it two days earlier was told the bot would wake it from deep sleep:
        # the right procedure announced as the one conclusion the evidence
        # argued against. A branch needs its own named outcome, not the absence
        # of the other branch's.
        "60+ days means sleep is likely": "not charged for 60+ days — deep sleep is the likely",
        "a recent charge means sleep is NOT likely": "deep sleep is not the likely explanation",
        "a recent charge points at a dead pack": "likely explanation on this branch is a dead",
        "never offer sleep as the explanation on a recent charge": "do not tell the customer you are waking it from sleep",
        "revival still runs on both branches": "revival process on either branch",
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
        # The gate, and it is step one. This record was applied to a bike that
        # would not turn on at all: the precondition was written as a
        # description ("the bike runs, the battery charges") next to a step
        # written as an instruction, and the instruction was what got executed.
        # The customer was asked whether the indicator had ever lit, sent the
        # four-part video that exists to prove a pack works, and told the bike
        # should still run — on a bike that was dead.
        #
        # Retrieval cannot arbitrate this. Both records own "SOC button" and
        # "no light"; the discriminator is whether the bike runs, which is in
        # the conversation and not in the query.
        "the gate is answered before anything else": "gate — answer this before anything else",
        "the discriminating question is named": "does the bike still run",
        "a dead bike leaves the record entirely": "this is not the record",
        "and is told where to go instead": "go to battery-wont-power-on",
        "being retrieved is not evidence it applies": "not evidence that it applies",
        "distinguish from won't-power-on": "only an indicator fault when everything else works",
        "ask whether it ever worked": "never lit",
        "a short tap does nothing": "short tap",
        "one continuous take, not four clips": "single continuous take",
        # Each shot carries its reason. Listed as bare actions, the middle two
        # read as chores — "the charger disconnected" has no visible takeaway at
        # all — and the bot relayed them that way. Unplugging is what proves the
        # bike in the last shot is running on the pack and not on the charger,
        # which is the only thing the video is evidence of.
        "the take shows the bike running on the pack": "running on it",
        "unplugging is justified, not just instructed": "may be running on the charger",
        # And the last item was phrased as a question inside a list of things to
        # film, so it invited a typed answer instead of the shot.
        "no question is embedded in the shot list": "do not put a question inside that list",
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


class ImpactDamageFlowTests(unittest.TestCase):
    """An impact re-routes the case, so the questions that decide it are pinned.

    Two of these are decisions rather than procedure, and both were taken
    deliberately against a plausible alternative — which is exactly the kind of
    rule a later rewrite drops without noticing it was a choice:

    * A crash with an intact-looking pack goes down the **normal** flow. The
      alternative was treating any impact to the battery as a safety case on the
      grounds that a knocked cell can be damaged internally with a clean
      exterior. Rejected: it would route every customer who dropped their bike to
      a service centre, most of them with a flat battery.
    * A damaged pack is still not to be charged while the case is open, even
      superficial-looking, even if the bike runs. That is the concession that
      makes the rule above safe, and it is the half most likely to be trimmed.
    """

    RECORD_ID = "battery-impact-damage"
    RULES = {
        "a live event outranks the impact assessment": "active hazard first",
        "never photograph a pack that is venting now": "do not ask for a photograph of it",
        "ask whether damage is visible at all": "visible damage",
        "no visible damage returns to the normal flow": "carry on with the normal investigation",
        "immediate vs delayed failure is asked separately": "stop right after",
        "a delayed failure must not override the rest": "do not let it override",
        "where it landed names the suspect": "names the suspect",
        "never assume the layout — ask": "layouts differ between models",
        "a damaged pack is the battery until proven otherwise": "until proven otherwise",
        "a damaged pack is not charged meanwhile": "not to be charged",
        "say what the photo cannot settle": "a person needs to look at it",
    }

    def test_every_rule_is_present(self):
        body = _body(self.RECORD_ID)
        missing = [name for name, phrase in self.RULES.items() if phrase not in body]
        self.assertEqual(missing, [], "rules lost: %s" % missing)

    def test_it_reaches_an_impact_report_in_english_and_hinglish(self):
        from emotorad_ai.knowledge import KnowledgeBase

        kb = KnowledgeBase()
        for query in ("i had an accident", "bike gir gaya", "dropped my cycle yesterday"):
            found = [p.id for p in kb.search(query, topic="battery", bike={"throttle": "yes"})]
            self.assertIn(self.RECORD_ID, found, query)

    def test_it_applies_to_every_bike_including_a_doodle(self):
        # A Doodle is dropped like anything else. Nothing in this record depends
        # on hardware a Doodle lacks, so an exclusion here would be a gap.
        record = next(r for r in load_records() if r.id == self.RECORD_ID)
        self.assertEqual(record.applies_to, {})
        self.assertEqual(record.excludes, {})


class WarrantyReplacementFlowTests(unittest.TestCase):
    """The destination four other flows had been pointing at for as long as they existed.

    "Move to warranty and replacement" ended the won't-power-on flow, the Doodle
    flow, the on/off switch flow and the SOC indicator flow, and no record
    described it — it was open item 4 in the prompt. A model that reaches a named
    destination which is not there does not stop, it improvises: after four
    checks that had settled the fault, it produced "this points to a deeper
    battery fault that needs to be checked by our service team" and asked
    permission to raise a ticket.

    The four coverage outcomes below must never collapse into each other. Telling
    an unregistered customer they are out of warranty, or an outage-hit customer
    to come back later forever, are different failures with the same cause.
    """

    RECORD_ID = "battery-warranty-replacement"
    RULES = {
        "only entered once a fault is concluded": "does not find one",
        "the part being replaced is named and does not drift": "say what is being replaced",
        "coverage comes from context, not another round of questions": "already in the customer context",
        "covered means act, not ask": "do not ask whether to raise it",
        "the 24-month term is provisional and said so near the edge": "provisional",
        "never announce a free replacement on an inferred date": "date you inferred",
        "missing purchase date is not the same as uncovered": "not the same\n    as not being covered",
        "no warranty record is not the same as out of warranty": "do not tell them they are out of warranty",
        "an outage is ours and never reads as a refusal": "never let an outage read as a refusal",
        "genuinely out of coverage is said once, with the paid route": "paid for",
        "a claim is opened, never a dispatch promised": "never promised as dispatched",
    }

    def test_every_rule_is_present(self):
        body = _body(self.RECORD_ID)
        missing = [
            name for name, phrase in self.RULES.items()
            if " ".join(phrase.split()) not in " ".join(body.split())
        ]
        self.assertEqual(missing, [], "rules lost: %s" % missing)

    def test_every_flow_that_ends_in_replacement_names_this_record(self):
        # The pointers are what make it reachable; a flow that ends at an
        # unnamed "warranty and replacement" is the defect this record exists
        # to close, and it would come back silently.
        import re

        for record in load_records():
            if record.id == self.RECORD_ID:
                continue
            body = " ".join(record.steps) + " " + record.escalate_when
            if re.search(r"replacement|warranty flow", body, re.I):
                self.assertIn(self.RECORD_ID, body, record.id)

    def test_it_is_reachable_in_english_and_hinglish(self):
        from emotorad_ai.knowledge import KnowledgeBase

        kb = KnowledgeBase()
        for query in ("battery replacement under warranty", "warranty mein replacement milega"):
            found = [p.id for p in kb.search(query, topic="battery", bike={"throttle": "yes"})]
            self.assertIn(self.RECORD_ID, found, query)


class CoverageAndCostTests(unittest.TestCase):
    """Who pays is two questions, and the second one kept being dropped.

    From the service engineer's case notes. Coverage alone does not decide cost:
    in warranty with no physical damage is free, any impact is chargeable even
    inside the warranty, and out of warranty is chargeable either way. There is
    no age rule within the term — `soc-indicator-dead` used to carry one ("under
    a year, replacement; a year or more, a paid service-centre repair") and it
    was wrong, so it is asserted gone rather than merely corrected.
    """

    def test_cost_turns_on_damage_and_coverage_not_on_age(self):
        body = _body("battery-warranty-replacement")
        for phrase in (
            "who pays is two questions",
            "chargeable even inside the warranty",
            "there is no age rule inside the warranty",
        ):
            self.assertIn(phrase, " ".join(body.split()), phrase)

    def test_the_bot_never_adjudicates_honesty(self):
        # It reports the photographs and what it was told, and lets a person
        # settle a disagreement between them.
        for record_id in ("battery-warranty-replacement", "battery-impact-damage"):
            body = " ".join(_body(record_id).split())
            self.assertIn("not", body)
            self.assertTrue(
                "telling the truth" in body,
                "%s must refuse to adjudicate an account" % record_id,
            )

    def test_no_record_still_carries_the_one_year_split(self):
        for record in load_records():
            body = " ".join((" ".join(record.steps) + " " + record.escalate_when).split()).lower()
            if "under a year" in body:
                self.assertIn(
                    "no such rule", body,
                    "%s still applies an age rule inside the warranty" % record.id,
                )

    def test_impact_is_chargeable_however_it_happened(self):
        body = " ".join(_body("battery-impact-damage").split())
        self.assertIn("impact damage is chargeable", body)
        self.assertIn("makes no difference", body)

    def test_a_figure_is_never_quoted(self):
        # No price list reaches the agent, so the service centre confirms cost.
        body = " ".join(_body("battery-warranty-replacement").split())
        self.assertIn("cannot quote a figure", body)


class ArrivalDamageTests(unittest.TestCase):
    """Scratches on a just-delivered pack are not a replacement, and are not nothing."""

    RECORD_ID = "battery-arrival-damage"
    RULES = {
        "whether it works is settled before the damage is discussed": "whether the battery works",
        "a dead pack on arrival is replaced, free": "did not get one",
        "breakage is never cosmetic": "also not cosmetic",
        "scratches alone are not offered a replacement first": "do not offer a\n    replacement as the first move",
        "the apology is unhedged": "apologise properly and without hedging",
        "three attempts, each a different argument": "each one a different argument",
        "repetition is named as the failure mode": "not persuading, it is stonewalling",
        "a customer who holds firm gets it, free and without friction": "do not make them ask a\n    fourth time",
        "it is ticketed even when nothing is replaced": "record it whatever they decide",
        "so arrival marks cannot be held against them later": "set the customer up to be refused later",
        "never argue the marks are not there": "they are looking at them",
    }

    def test_every_rule_is_present(self):
        body = " ".join(_body(self.RECORD_ID).split())
        missing = [
            name for name, phrase in self.RULES.items()
            if " ".join(phrase.split()) not in body
        ]
        self.assertEqual(missing, [], "rules lost: %s" % missing)


class SwitchNotCuttingOutputTests(unittest.TestCase):
    """The inverse of the dead-switch record, and confusable with it."""

    RECORD_ID = "battery-switch-not-cutting-output"
    RULES = {
        "the shape is confirmed against the opposite fault": "battery-onoff-switch-dead",
        "the switch being operated is filmed, not described": "on camera",
        "a normal fade is not this fault": "capacitor drain",
        "replacement is the only remedy": "repair is not offered for this one",
        "malfunction must not drift into damage": "opposite answers on who pays",
        "a pack that cannot be isolated is not left charging unattended": "charging unattended",
    }

    def test_every_rule_is_present(self):
        body = " ".join(_body(self.RECORD_ID).split())
        missing = [name for name, p in self.RULES.items() if " ".join(p.split()) not in body]
        self.assertEqual(missing, [], "rules lost: %s" % missing)

    def test_it_is_scoped_away_from_doodle(self):
        record = next(r for r in load_records() if r.id == self.RECORD_ID)
        self.assertEqual(record.excludes.get("product_name"), "doodle")


class FlowHandoffTests(unittest.TestCase):
    """A flow that says "move on to the other parts" must say which, and where.

    It used to say exactly that and stop. The step is reached whenever the pack
    is healthy and the bike still will not power on — a real conversation, not a
    corner — and with nothing named, the model had to invent both the next part
    and how to check it. It invented "look at the connector for melting", which
    is right, and then asked for one photo of one end with nothing to compare
    against, because it had no reason to know `battery-melted-terminal` existed:
    that record sets the order, requires both ends, and carries the
    melted-versus-normal pictures. Everything it needed was authored and
    unreachable from where it stood.

    Records live in separate files and none imports another, so a pointer that
    goes stale breaks in silence. Same reason as the E-06 test below.
    """

    POINTS_AT = "battery-melted-terminal"
    HANDOFF_SOURCES = ("battery-wont-power-on", "battery-doodle-wont-power-on")

    def test_the_target_record_exists(self):
        self.assertIn(self.POINTS_AT, {r.id for r in load_records()})

    def test_a_healthy_battery_leads_somewhere_by_name(self):
        for record_id in self.HANDOFF_SOURCES:
            self.assertIn(self.POINTS_AT, _body(record_id), record_id)

    def test_the_target_still_carries_what_the_handoff_promises(self):
        # The pointer is only worth having while these hold. Both ends, in
        # order, each asked for with its comparison picture.
        body = _body(self.POINTS_AT)
        for promise in ("two photos", "first, the battery terminal", "second, the controller"):
            self.assertIn(promise, body, promise)
        media = {
            item.get("id")
            for record in load_records()
            if record.id == self.POINTS_AT
            for item in record.media
        }
        self.assertEqual(media, {"melted_battery_vs_non_melted", "controller_melted_vs_non_melted"})


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

    def test_the_error_code_points_at_the_record_instead_of_restating_it(self):
        """One procedure, one place. The code table names it and stops.

        `verification` used to restate the whole check — two photos, terminal
        first, controller either way — which made it a second copy of this
        record. The copy is what got followed: §5b0 has `lookup_error_code` run
        before anything else, its answer read as complete instructions, and
        `search_knowledge` was never called at all. So the customer was asked to
        judge a melted terminal with no comparison picture to judge it against
        (this field cannot carry one), and the case was closed on the first
        answer with the controller never looked at.
        """
        from emotorad_ai.errorcodes import load_table

        entry = load_table().lookup("E-06", "X2 Furious Red V2")["entry"]
        verification = entry["verification"]
        self.assertIn(self.RECORD_ID, verification, "must name the record that owns the check")
        self.assertIn("search", verification.lower(), "must require the lookup, not replace it")
        from emotorad_ai.knowledge import KnowledgeBase

        found = KnowledgeBase(load_records()).search(
            "melted charging port E-06", topic="battery", bike={"throttle": "yes"}
        )
        self.assertIn(self.RECORD_ID, [p.id for p in found])

