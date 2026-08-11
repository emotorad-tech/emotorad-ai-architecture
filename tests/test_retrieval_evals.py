"""Retrieval evals — scored separately from conversations, on purpose.

The reason this file exists apart from the conversation tests: **retrieval fails
silently.** Hand the model the wrong passage and it writes a fluent, confident,
well-structured answer that reads exactly like a right one. A conversational
review passes it. A human reviewer skimming transcripts passes it. Only a check
that asserts *which record came back* catches it.

So this is measured as a set with a floor, not as isolated assertions — the
number is what tells you whether an authoring change made retrieval better or
quietly worse.
"""

import unittest

from emotorad_ai.knowledge import KnowledgeBase, KnowledgeRecord, load_records

# (query, expected record id, topic, bike). Phrasings are how customers actually
# write — Hinglish, Devanagari, and vague openers — not how a technical writer
# would. `bike` matters because retrieval is filtered by what the person owns.
ANY_BIKE = {"throttle": "yes"}
GOLDEN = [
    # -- battery ------------------------------------------------------------
    ("my battery is not charging at all", "battery-wont-charge", "battery", ANY_BIKE),
    ("charger light is not coming on", "battery-wont-charge", "battery", ANY_BIKE),
    ("charger laga diya but nothing happens", "battery-wont-charge", "battery", ANY_BIKE),
    ("battery charge nahi ho rahi", "battery-wont-charge", "battery", ANY_BIKE),
    ("I am getting much less backup than before", "battery-range-dropped", "battery", ANY_BIKE),
    ("range has dropped a lot recently", "battery-range-dropped", "battery", ANY_BIKE),
    ("battery draining quickly", "battery-range-dropped", "battery", ANY_BIKE),
    ("bike is not turning on at all, display is blank", "battery-wont-power-on", "battery", ANY_BIKE),
    ("no power, nothing happens when I press the button", "battery-wont-power-on", "battery", ANY_BIKE),
    ("charging is taking too long", "battery-charging-slowly", "battery", ANY_BIKE),
    ("slow charging problem", "battery-charging-slowly", "battery", ANY_BIKE),
    ("I am not using the bike for three months, what to do", "battery-storage", "battery", ANY_BIKE),
    ("winter storage advice", "battery-storage", "battery", ANY_BIKE),
    # -- motor --------------------------------------------------------------
    ("motor is making a grinding noise", "motor-noise", "motor", ANY_BIKE),
    ("there is a whining sound from the motor", "motor-noise", "motor", ANY_BIKE),
    ("motor se awaz aa rahi hai", "motor-noise", "motor", ANY_BIKE),
    ("pedal assist is not working", "motor-no-assist", "motor", ANY_BIKE),
    ("no power assist when I pedal", "motor-no-assist", "motor", ANY_BIKE),
    ("pas dead, motor not running", "motor-no-assist", "motor", ANY_BIKE),
    ("power cuts out while riding", "motor-cuts-out", "motor", ANY_BIKE),
    ("motor stops intermittently on bumps", "motor-cuts-out", "motor", ANY_BIKE),
    ("jerking and power cutting off", "motor-cuts-out", "motor", ANY_BIKE),
    ("throttle is not responding", "motor-throttle", "motor", ANY_BIKE),
    ("accelerator dead", "motor-throttle", "motor", ANY_BIKE),
    # -- Hindi / Hinglish, reported separately and never averaged in -----------
    ("बैटरी चार्ज नहीं हो रही", "battery-wont-charge", "battery", ANY_BIKE),
    ("मोटर से आवाज आ रही है", "motor-noise", "motor", ANY_BIKE),
    ("battery ka backup kam ho gaya hai", "battery-range-dropped", "battery", ANY_BIKE),
]

# Queries with no good answer in the corpus. Returning nothing is the correct
# result — a confident near-miss is worse than an admitted gap.
OUT_OF_SCOPE = [
    "what is the price of a new bike",
    "where is my order",
    "can I get a refund",
    "who is the CEO of emotorad",
]

# The floor. Set below current performance so an authoring change that helps one
# query and hurts two is caught, rather than being absorbed silently.
MIN_TOP1 = 0.85
MIN_TOP2 = 0.95


class GoldenSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def _hits(self, query, topic, k=2, bike=None):
        return [p.id for p in self.kb.search(query, top_k=k, topic=topic, bike=bike or ANY_BIKE)]

    def test_top1_accuracy_meets_the_floor(self):
        misses = [
            (query, expected, self._hits(query, topic, k=1, bike=bike))
            for query, expected, topic, bike in GOLDEN
            if self._hits(query, topic, k=1, bike=bike)[:1] != [expected]
        ]
        accuracy = 1 - len(misses) / len(GOLDEN)
        self.assertGreaterEqual(
            accuracy, MIN_TOP1,
            "top-1 %.0f%% (floor %.0f%%). Misses: %s" % (accuracy * 100, MIN_TOP1 * 100, misses),
        )

    def test_top2_accuracy_meets_the_floor(self):
        misses = [
            (query, expected, self._hits(query, topic, bike=bike))
            for query, expected, topic, bike in GOLDEN
            if expected not in self._hits(query, topic, bike=bike)
        ]
        accuracy = 1 - len(misses) / len(GOLDEN)
        self.assertGreaterEqual(
            accuracy, MIN_TOP2,
            "top-2 %.0f%% (floor %.0f%%). Misses: %s" % (accuracy * 100, MIN_TOP2 * 100, misses),
        )

    def test_non_english_queries_are_reported_separately(self):
        # Never aggregate across languages. A 90% average can hide 100% English
        # and 40% Hindi, and the aggregate is what gets put on a slide.
        markers = ("nahi", "awaz", "laga diya", "kam ho gaya")
        non_english = [
            row for row in GOLDEN
            if not row[0].isascii() or any(marker in row[0] for marker in markers)
        ]
        self.assertGreaterEqual(len(non_english), 5, "the golden set needs non-English coverage")
        hits = [row for row in non_english if self._hits(row[0], row[2], bike=row[3])[:1] == [row[1]]]
        self.assertEqual(
            len(hits), len(non_english),
            "non-English misses: %s" % [r[0] for r in non_english if r not in hits],
        )

    def test_out_of_scope_queries_return_nothing(self):
        noisy = [
            (query, self.kb.search(query)) for query in OUT_OF_SCOPE if self.kb.search(query)
        ]
        self.assertEqual(noisy, [], "a confident near-miss is worse than an admitted gap")


class TopicFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def test_a_battery_query_scoped_to_motor_returns_no_battery_record(self):
        for passage in self.kb.search("battery not charging", topic="motor"):
            self.assertEqual(passage.record.topic, "motor")

    def test_every_record_is_reachable_by_at_least_one_golden_query(self):
        # An unreachable record is content someone wrote that no customer will
        # ever see — and nothing else in the suite would notice.
        reachable = set()
        for query, _, topic, bike in GOLDEN:
            reachable.update(
                p.id for p in self.kb.search(query, top_k=2, topic=topic, bike=bike)
            )
        orphans = {record.id for record in self.kb.records} - reachable
        self.assertEqual(orphans, set(), "records no golden query reaches: %s" % orphans)


class AppliesToFilterTests(unittest.TestCase):
    """`applies_to` must be a hard filter — wrong-for-this-bike is unretrievable."""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase()

    def test_a_throttle_record_is_invisible_to_a_bike_without_one(self):
        hits = self.kb.search("throttle not working", topic="motor", bike={"throttle": "no"})
        self.assertNotIn("motor-throttle", [p.id for p in hits])

    def test_the_same_record_is_retrievable_for_a_bike_with_one(self):
        hits = self.kb.search("throttle not working", topic="motor", bike={"throttle": "yes"})
        self.assertIn("motor-throttle", [p.id for p in hits])

    def test_an_unknown_bike_attribute_excludes_rather_than_assumes(self):
        # We cannot confirm the record applies. Excluding costs a possible answer;
        # including risks telling someone to check a part their bike lacks.
        hits = self.kb.search("throttle not working", topic="motor", bike={})
        self.assertNotIn("motor-throttle", [p.id for p in hits])

    def test_records_with_no_applies_to_are_universal(self):
        hits = self.kb.search("battery not charging", topic="battery", bike={})
        self.assertIn("battery-wont-charge", [p.id for p in hits])


class CorpusHealthTests(unittest.TestCase):
    """Properties of the authored content itself, checked at load."""

    @classmethod
    def setUpClass(cls):
        cls.records = load_records()

    def test_every_record_loads_and_validates(self):
        self.assertGreaterEqual(len(self.records), 9)

    def test_ids_are_unique_and_stable_looking(self):
        ids = [record.id for record in self.records]
        self.assertEqual(len(ids), len(set(ids)))
        for record in self.records:
            self.assertTrue(record.id.startswith(record.topic), record.id)

    def test_every_record_says_when_to_escalate(self):
        # Without this the agent troubleshoots forever rather than handing over.
        missing = [record.id for record in self.records if not record.escalate_when]
        self.assertEqual(missing, [], "records with no escalate_when: %s" % missing)

    def test_no_step_tells_a_customer_to_do_something_unsafe(self):
        forbidden = ("open the battery", "disassemble", "bypass", "cut the wire", "solder")
        offences = [
            (record.id, step)
            for record in self.records
            for step in record.steps
            if any(phrase in step.lower() for phrase in forbidden)
        ]
        self.assertEqual(offences, [])

    def test_every_media_item_has_a_caption(self):
        # A photo with no caption is invisible to retrieval and useless to the
        # model, which cannot see the image.
        for record in self.records:
            for item in record.media:
                self.assertTrue(item.get("caption"), record.id)

    def test_a_superseded_record_is_removed_from_the_index(self):
        # Deleted, not down-ranked: a stale record that still retrieves answers
        # with exactly the confidence of a current one.
        stale = KnowledgeRecord(
            id="battery-old", title="Old advice", topic="battery",
            symptoms=["not charging"], steps=["Do the old thing."],
            superseded_by="battery-wont-charge",
        )
        kb = KnowledgeBase([stale] + list(self.records))
        self.assertNotIn("battery-old", [p.id for p in kb.search("not charging")])
        self.assertEqual([r.id for r in kb.retired], ["battery-old"])


if __name__ == "__main__":
    unittest.main()
