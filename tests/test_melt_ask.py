"""The melt ask (the person's brief, 6 October 2026).

When a customer says something melted, the bot answers with one fixed message
written by code, asking for all three items at once (the battery's serial
sticker, the controller's label, a short video of both ends), with a reference
picture for each in the same reply, and tells them not to use or charge the
bike meanwhile. No model is called on that turn.

It is off unless EMOTORAD_MELT_ASK is exactly "on" and all three catalogue
pictures exist and resolve. The battery serial sticker has no photo yet, so in
the shipped catalogue it stays off.
"""

import unittest
from datetime import date

from emotorad_ai import media, melt_ask
from emotorad_ai.adapters import DealerWhatsAppAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import ANONYMOUS, VERIFIED, Identity, InboundMessage, Reply
from emotorad_ai.conversation import ConversationState, InMemoryConversationStore
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.disclosure import apply_disclosure
from emotorad_ai.evidence_asks import MAX_EVIDENCE_ASKS
from emotorad_ai.evidence_check import verdict_record
from emotorad_ai.guardrails import SAFETY_MESSAGE
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.knowledge import load_records
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.media import load_catalogue
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TOPIC_AGENTS, TURN_FACT_FIELDS, Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import build_registry
from emotorad_ai.tools.verification import VerificationStore
from emotorad_ai.triage import TriageAgent

TODAY = date(2026, 10, 6)
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus EMXP2025004417 (fixtures)
DOODLE = "+919812345678"  # Rohit, one Doodle V3 (fixtures)
TWO_BIKES = fixtures.PHONE_AMIIGO_TEST_RIDER  # EMX Plus EMXP2026001234 and a Doodle V3
THREE_BIKES = "+919700000001"  # T-Rex Air TREX2024881201, EMX Plus EMXP2025004990, Doodle V3
DEALER = "919000000001"  # Royal Cycle Stores (fixtures)
AGENT_REPLY = "Thanks. Could you send a short video of the charger plugged in?"

SERIAL_KEY = "afs/battery/photos/battery-serial-label.jpg"
SERIAL = {"id": SERIAL_KEY, "kind": "image", "caption": "Example: the serial number sticker on the battery",
          "code_only": True}
ON = {"EMOTORAD_MELT_ASK": "on"}

# The exact texts the person approved (6 October 2026). The Hindi is a draft
# for a Hindi speaker to check before real traffic.
TEXT_EN = (
    "Thanks for telling me. Until we've checked it, please don't use or charge the bike.\n"
    "\n"
    "To check it, please send these three together:\n"
    "1. A photo of the serial number sticker on your battery.\n"
    "2. A photo of the controller's label, on the frame where the battery slots in.\n"
    "3. A short video of the battery's metal terminals and the connector on the frame. "
    "Hold the camera still on each for a second.\n"
    "\n"
    "The pictures below show what each one looks like."
)
TEXT_HI = (
    "बताने के लिए धन्यवाद। जब तक हम इसकी जाँच न कर लें, कृपया बाइक न चलाएँ और न ही चार्ज करें।\n"
    "\n"
    "जाँच के लिए कृपया ये तीनों एक साथ भेजें:\n"
    "1. आपकी बैटरी पर लगे सीरियल नंबर स्टिकर की फ़ोटो।\n"
    "2. फ़्रेम पर, जहाँ बैटरी लगती है, वहाँ कंट्रोलर के लेबल की फ़ोटो।\n"
    "3. बैटरी के धातु वाले टर्मिनल और फ़्रेम के कनेक्टर का एक छोटा वीडियो। "
    "हर एक पर कैमरा एक सेकंड स्थिर रखें।\n"
    "\n"
    "नीचे दी गई तस्वीरों में हर एक का उदाहरण है।"
)

GOLDEN = (
    "something is melted in battery",
    "my battery connector is melted",
    "charging port melted",
    "connector melt ho gaya",
    "terminal pighal gaya",
    "बैटरी का टर्मिनल पिघल गया",
    "E-06",
    # Case, and the code inside a sentence.
    "MY CONNECTOR IS MELTED",
    "the display shows e-06 while charging",
    "pins fused",
)
NEAR_MISSES = ("unmelted", "meltdown", "my battery won't charge", "the bike is fine", "E-061", "XE-06")


class FakeStore:
    """Signs every key, or fails for the keys named in `fail`."""

    def __init__(self, fail=()):
        self.fail = set(fail)

    def presign_get(self, key):
        if any(part in key for part in self.fail):
            raise OSError("cannot sign %s" % key)
        return "https://signed.test/" + key


def full_catalogue():
    """The shipped catalogue with the battery serial picture added, as the
    runbook says to once the photo exists."""
    return dict(load_catalogue(), melt_battery_serial=dict(SERIAL))


def active(store=None):
    ask, status = melt_ask.from_env(full_catalogue(), store or FakeStore(), ON)
    assert ask is not None, status
    return ask


class TriggerTests(unittest.TestCase):
    def setUp(self):
        self.pattern = melt_ask.compile_trigger(melt_ask.trigger_phrases())

    def test_the_golden_phrases_trigger(self):
        for text in GOLDEN:
            with self.subTest(text=text):
                self.assertTrue(melt_ask.triggered(self.pattern, text))

    def test_the_near_misses_do_not(self):
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assertFalse(melt_ask.triggered(self.pattern, text))

    def test_the_phrases_are_the_records_symptoms_and_the_devanagari_list(self):
        (record,) = [r for r in load_records() if r.id == "battery-melted-terminal"]
        phrases = melt_ask.trigger_phrases()
        self.assertEqual(set(phrases), set(record.symptoms) | {"पिघल गया", "पिघल गई", "पिघला", "पिघल गए", "मेल्ट"})
        for phrase in ("पिघल गई", "पिघल गए", "मेल्ट"):
            self.assertTrue(melt_ask.triggered(self.pattern, "टर्मिनल " + phrase), phrase)

    def test_the_pattern_uses_neither_word_boundaries_nor_word_classes(self):
        # Devanagari vowel signs fall outside \\w, so \\b and \\w miss Hindi
        # (the rule in CLAUDE.md). ASCII phrases use explicit lookarounds.
        self.assertNotIn("\\b", self.pattern.pattern)
        self.assertNotIn("\\w", self.pattern.pattern)
        self.assertIn("(?<![a-z0-9])", self.pattern.pattern)
        self.assertIn("(?![a-z0-9])", self.pattern.pattern)

    def test_empty_text_does_not_trigger(self):
        self.assertFalse(melt_ask.triggered(self.pattern, ""))
        self.assertFalse(melt_ask.triggered(self.pattern, None))


class SwitchTests(unittest.TestCase):
    def test_on_only_when_exactly_on(self):
        self.assertTrue(melt_ask.switch_on({"EMOTORAD_MELT_ASK": "on"}))
        for value in ("", "ON", "On", " on", "yes", "true", "1"):
            with self.subTest(value=value):
                self.assertFalse(melt_ask.switch_on({"EMOTORAD_MELT_ASK": value}))
        self.assertFalse(melt_ask.switch_on({}))

    def test_off_by_default(self):
        self.assertEqual(melt_ask.from_env(full_catalogue(), FakeStore(), {}), (None, "off"))

    def test_on_but_the_battery_serial_picture_missing_is_off_and_says_so(self):
        # The shipped catalogue today: no serial sticker photo exists yet.
        self.assertEqual(melt_ask.from_env(load_catalogue(), FakeStore(), ON),
                         (None, "off: missing melt_battery_serial"))

    def test_on_with_all_three_resolving_is_on(self):
        ask, status = melt_ask.from_env(full_catalogue(), FakeStore(), ON)
        self.assertEqual(status, "on")
        self.assertEqual([key for key, _ in ask.items], list(melt_ask.KEYS))

    def test_a_picture_that_cannot_resolve_keeps_it_off_and_is_named(self):
        ask, status = melt_ask.from_env(full_catalogue(), FakeStore(fail=("battery-terminals",)), ON)
        self.assertIsNone(ask)
        self.assertEqual(status, "off: unresolvable melt_terminals")

    def test_with_no_media_store_none_resolves(self):
        # No bucket on this deployment: media.resolve falls back to the store
        # from the environment, pinned here to none.
        previous = (media._store, media._store_loaded)
        media._store, media._store_loaded = None, True
        try:
            ask, status = melt_ask.from_env(load_catalogue(), None, ON)
        finally:
            media._store, media._store_loaded = previous
        self.assertIsNone(ask)
        self.assertEqual(status, "off: missing melt_battery_serial; unresolvable melt_controller_label, melt_terminals")

    def test_without_the_knowledge_record_it_is_off_and_says_so(self):
        self.assertEqual(melt_ask.from_env(full_catalogue(), FakeStore(), ON, records=[]),
                         (None, "off: no knowledge record battery-melted-terminal"))

    def test_a_melt_picture_the_model_could_be_offered_keeps_it_off(self):
        catalogue = full_catalogue()
        catalogue["melt_battery_serial"] = dict(SERIAL, code_only=False)
        self.assertEqual(melt_ask.from_env(catalogue, FakeStore(), ON), (None, "off: not code_only melt_battery_serial"))


class TextAndPictureTests(unittest.TestCase):
    def test_the_texts_are_the_approved_ones(self):
        self.assertEqual(melt_ask.TEXT_EN, TEXT_EN)
        self.assertEqual(melt_ask.TEXT_HI, TEXT_HI)
        self.assertNotIn(chr(0x2014), TEXT_EN + TEXT_HI)

    def test_the_pictures_come_in_order_serial_controller_terminals(self):
        pictures, missing = active().pictures()
        self.assertEqual(missing, [])
        self.assertEqual([p.url for p in pictures], [
            "https://signed.test/assets/afs/battery/photos/battery-serial-label.w900.webp",
            "https://signed.test/assets/afs/battery/photos/controller-pins-closeup.w900.webp",
            "https://signed.test/assets/afs/battery/photos/battery-terminals.w900.webp",
        ])
        self.assertEqual({p.kind for p in pictures}, {"image"})
        self.assertEqual(pictures[1].caption, "Example: the controller's label, next to the battery pins on the frame")

    def test_a_picture_that_fails_at_send_time_is_left_out_and_named(self):
        store = FakeStore()
        ask = active(store)
        store.fail.add("controller-pins")
        pictures, missing = ask.pictures()
        self.assertEqual(len(pictures), 2)
        self.assertEqual(missing, ["melt_controller_label"])


# --- in a conversation, through runtime.handle() ------------------------------

SERIAL_URL = "https://signed.test/assets/afs/battery/photos/battery-serial-label.w900.webp"
CONTROLLER_URL = "https://signed.test/assets/afs/battery/photos/controller-pins-closeup.w900.webp"
TERMINALS_URL = "https://signed.test/assets/afs/battery/photos/battery-terminals.w900.webp"


def first_reply(text, channel="website_chat"):
    """The text as the first reply of a conversation carries it: with the AI
    disclosure in front (disclosure.py)."""
    return apply_disclosure(text, ConversationState(conversation_id="x"), channel)


class MeltChat:
    """One customer, with the melt ask on (the default here) or off. Any other
    keyword goes to the Runtime (Jev and the narrow model, for one)."""

    def __init__(self, replies=(), ask=True, phone=ONE_BIKE, select=None, routed=None, store=None, **runtime_kwargs):
        self.registry = build_registry(today=TODAY)
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.store = store or FakeStore()
        self.phone = phone
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=self.registry, llm=self.llm,
            log=self.log, resolver=IdentityResolver(self.registry), conversations=self.conversations,
            melt_ask=active(self.store) if ask else None, **runtime_kwargs,
        )
        state = self.conversations.get("c1")
        if select:
            state.select_bike(select, "EMX Plus")
        if routed:
            state.route_to(routed)

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=VERIFIED, phone=self.phone, em_aid="aid-1"),
        ))

    def state(self):
        return self.conversations.peek("c1")

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]


class MeltAskTests(unittest.TestCase):
    def test_a_melt_with_a_bike_chosen_gets_the_fixed_ask_and_three_pictures_and_no_model(self):
        chat = MeltChat(select="EMXP2025004417", routed="battery_support")
        reply = chat.say("something is melted in battery")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.text, first_reply(melt_ask.TEXT_EN))
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertFalse(reply.escalated)
        self.assertEqual([a.url for a in reply.attachments], [SERIAL_URL, CONTROLLER_URL, TERMINALS_URL])
        state = chat.state()
        self.assertEqual(state.agent, "battery_support")
        self.assertEqual(state.fault_topic, "battery")
        self.assertEqual(state.evidence_asks, 1)
        self.assertEqual(state.melt_asked_frames, ["EMXP2025004417"])
        self.assertFalse(state.melt_pending)
        # The model sees the ask on its next turn, as the bot's own words.
        self.assertEqual(state.history[-1]["content"][0]["text"], reply.text)

    def test_the_first_message_of_a_one_bike_customer_gets_it_too(self):
        # Triage chooses the only bike and routes to the battery agent in the
        # same turn; the ask goes out instead of the agent.
        chat = MeltChat()
        reply = chat.say("my battery connector is melted")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.text, first_reply(melt_ask.TEXT_EN))
        self.assertEqual(len(reply.attachments), 3)
        self.assertEqual(chat.state().melt_asked_frames, ["EMXP2025004417"])

    def test_a_melt_in_devanagari_gets_the_hindi_text(self):
        chat = MeltChat(select="EMXP2025004417", routed="battery_support")
        reply = chat.say("बैटरी का टर्मिनल पिघल गया")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.text, first_reply(melt_ask.TEXT_HI))
        self.assertEqual(len(reply.attachments), 3)

    def test_hinglish_gets_the_english_text(self):
        chat = MeltChat(select="EMXP2025004417", routed="battery_support")
        self.assertEqual(chat.say("terminal pighal gaya").text, first_reply(melt_ask.TEXT_EN))

    def test_it_is_logged_without_a_frame_number_a_phone_or_a_url(self):
        chat = MeltChat(select="EMXP2025004417", routed="battery_support")
        chat.say("something is melted in battery")
        (event,) = chat.events("melt_ask")
        self.assertEqual((event["frame_present"], event["language"], event["pictures"]), (True, "en", 3))
        flat = repr(event)
        for secret in ("EMXP2025004417", "9876543210", "https://", "signed.test"):
            self.assertNotIn(secret, flat)

    def test_burnt_is_a_safety_stop_and_no_melt_ask(self):
        for text in ("terminal burnt", "jal gaya", "there is a burnt smell", "it got very hot and melted"):
            with self.subTest(text=text):
                chat = MeltChat(select="EMXP2025004417", routed="battery_support")
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "guardrail:battery_safety")
                self.assertIn(SAFETY_MESSAGE, reply.text)
                self.assertEqual(reply.attachments, [])
                self.assertEqual(chat.events("melt_ask"), [])
                self.assertFalse(chat.state().melt_pending)
                self.assertEqual(chat.state().melt_asked_frames, [])

    def test_switched_off_a_melt_goes_to_the_battery_agent_as_today(self):
        chat = MeltChat([say(AGENT_REPLY)], ask=False, select="EMXP2025004417", routed="battery_support")
        reply = chat.say("something is melted in battery")
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertIn(AGENT_REPLY, reply.text)
        self.assertEqual(reply.attachments, [])
        self.assertEqual(chat.events("melt_ask"), [])
        self.assertFalse(chat.state().melt_pending)

    def test_a_doodle_goes_to_the_battery_agent(self):
        # The references show the downtube battery; a Doodle's connector differs.
        chat = MeltChat([say(AGENT_REPLY)], phone=DOODLE)
        reply = chat.say("something is melted in battery")
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(reply.attachments, [])
        self.assertEqual(chat.state().melt_asked_frames, [])
        self.assertFalse(chat.state().melt_pending)

    def test_a_dealer_never_gets_it(self):
        chat = MeltChat([say("Which order is that about?")])
        reply = chat.runtime.handle(DealerWhatsAppAdapter(chat.runtime.resolver).to_message(
            {"from": DEALER, "text": "my battery connector is melted", "conversation_id": "d1"}))
        self.assertEqual(reply.handled_by, "dealer_orders")
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertEqual(reply.attachments, [])
        self.assertEqual(chat.events("melt_ask"), [])
        self.assertFalse(chat.conversations.peek("d1").melt_pending)

    def test_moving_to_the_dealer_persona_clears_a_pending_ask(self):
        chat = MeltChat([say("Which order is that about?")])
        chat.conversations.get("d1").melt_pending = True
        chat.runtime.handle(DealerWhatsAppAdapter(chat.runtime.resolver).to_message(
            {"from": DEALER, "text": "need stock", "conversation_id": "d1"}))
        self.assertFalse(chat.conversations.peek("d1").melt_pending)

    def test_a_second_melt_for_the_same_bike_goes_to_the_agent(self):
        chat = MeltChat([say(AGENT_REPLY)], select="EMXP2025004417", routed="battery_support")
        chat.say("something is melted in battery")
        again = chat.say("yes the connector is melted, I told you")
        self.assertEqual(again.handled_by, "battery_support")
        self.assertIn(AGENT_REPLY, again.text)
        self.assertEqual(again.attachments, [])
        self.assertEqual(len(chat.events("melt_ask")), 1)
        self.assertEqual(chat.state().evidence_asks, 1 + 1)  # the agent's ask for a video counts too

    def test_another_bike_chosen_and_melted_again_gets_its_own_ask(self):
        chat = MeltChat([say(AGENT_REPLY)], phone=THREE_BIKES)
        self.assertEqual(chat.say("my battery terminal is melted").handled_by, "triage")
        first = chat.say("T-Rex Air")
        self.assertEqual(first.handled_by, "melt_ask")
        self.assertEqual(chat.state().melt_asked_frames, ["TREX2024881201"])
        self.assertEqual(chat.say("other bike").handled_by, "navigation:change_bike")
        second = chat.say("EMX Plus, its connector is melted too")
        self.assertEqual(second.handled_by, "melt_ask")
        self.assertEqual(second.text, melt_ask.TEXT_EN)
        self.assertEqual(len(second.attachments), 3)
        self.assertEqual(chat.state().melt_asked_frames, ["TREX2024881201", "EMXP2025004990"])
        self.assertEqual(chat.llm.requests, [])

    def test_a_melt_before_the_bike_is_chosen_is_asked_once_it_is(self):
        chat = MeltChat(phone=TWO_BIKES)
        which = chat.say("my battery terminal is melted")
        self.assertEqual(which.handled_by, "triage")
        self.assertEqual(which.attachments, [])
        self.assertTrue(chat.state().melt_pending)
        reply = chat.say("EMX Plus")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertEqual(reply.text, melt_ask.TEXT_EN)
        self.assertEqual([a.url for a in reply.attachments], [SERIAL_URL, CONTROLLER_URL, TERMINALS_URL])
        self.assertEqual(chat.state().melt_asked_frames, ["EMXP2026001234"])
        self.assertFalse(chat.state().melt_pending)

    def test_a_pending_ask_in_devanagari_is_sent_in_hindi_once_the_bike_is_chosen(self):
        chat = MeltChat(phone=TWO_BIKES)
        chat.say("बैटरी का टर्मिनल पिघल गया")
        self.assertEqual(chat.say("EMX Plus").text, melt_ask.TEXT_HI)

    def test_a_pending_ask_for_a_doodle_is_dropped(self):
        chat = MeltChat([say(AGENT_REPLY)], phone=TWO_BIKES)
        chat.say("my battery terminal is melted")
        reply = chat.say("Doodle V3")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertFalse(chat.state().melt_pending)
        self.assertEqual(chat.state().melt_asked_frames, [])

    def test_a_safety_stop_clears_a_pending_ask(self):
        # Safety wins: after the safety reply nobody is asked to film the bike.
        chat = MeltChat([say(AGENT_REPLY)], phone=TWO_BIKES)
        chat.say("my battery terminal is melted")
        self.assertEqual(chat.say("now it is smoking").handled_by, "guardrail:battery_safety")
        self.assertFalse(chat.state().melt_pending)

    def test_one_picture_that_will_not_resolve_leaves_the_text_and_the_others(self):
        chat = MeltChat(select="EMXP2025004417", routed="battery_support")
        chat.store.fail.add("controller-pins")
        reply = chat.say("something is melted in battery")
        self.assertEqual(reply.text, first_reply(melt_ask.TEXT_EN))
        self.assertEqual([a.url for a in reply.attachments], [SERIAL_URL, TERMINALS_URL])
        (missing,) = chat.events("melt_ask_media_missing")
        self.assertEqual(missing["keys"], ["melt_controller_label"])
        self.assertNotIn("https://", repr(missing))
        self.assertEqual(chat.events("melt_ask")[0]["pictures"], 2)

    def test_on_the_web_chat_a_melt_before_verifying_is_asked_once_the_bike_is_chosen(self):
        store = VerificationStore()
        registry = build_registry(verification=store, today=TODAY, account_finder=fixtures.find_account_by_order_code)
        llm = ScriptedClaude([])
        conversations = InMemoryConversationStore()
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
            log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=conversations,
            self_service_identity=True, phone_resolver=store.verified_phone, otp_verified_at=store.verified_on,
            verify_first=True, melt_ask=active(),
        )

        def send(text):
            return runtime.handle(InboundMessage(
                conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
                identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

        self.assertEqual(send("my battery terminal is melted").handled_by, "verify_first:ask_number")
        send(TWO_BIKES[3:])
        self.assertEqual(send(store.pending_code("c1")).handled_by, "verify_first:verified")
        reply = send("EMX Plus")
        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertEqual(len(reply.attachments), 3)
        self.assertEqual(conversations.peek("c1").melt_asked_frames, ["EMXP2026001234"])


# --- the fix round (the controller's rulings of 6 October 2026) ---------------

# Ruling 4: what the battery and narrow agents are told after the ask, verbatim.
ASKED_NOTE = (
    "The customer was already asked, in one message, for a photo of the battery serial sticker, a photo of the "
    "controller's label and a video of the battery terminals and the frame's connector. Judge what they send. "
    "Do not ask again for the ends one at a time; if something is missing or unclear, ask only for that item."
)
LATER_REPLY = "Thanks for letting me know. Is there anything else about the bike I can help with?"
MOTOR_REPLY = "Thanks. Does the motor stop completely, or does it cut in and out?"
FRAME = "EMXP2025004417"  # ONE_BIKE's EMX Plus

# Ruling 3: the motor named and no battery word is a motor melt.
MOTOR_NAMED = ("motor", "Motor", "MOTOR", "the motors", "hub", "hub motor", "the hub's wire", "मोटर", "मोटर का")
NOT_MOTOR = ("EMotorad", "my emotorad bike", "motorbike", "github", "chub", "hubby", "")
BATTERY_NAMED = ("battery", "Battery", "batteries", "terminal", "terminals", "charging port", "charging  ports",
                 "charger", "chargers", "बैटरी", "टर्मिनल", "बैटरी का")
NOT_BATTERY = ("batteryless", "terminally", "supercharger", "charging", "port", "connector", "")
MOTOR_MELTS = ("my motor connector is melted", "the hub connector melted", "Motor wire pighal gaya",
               "मोटर का कनेक्टर पिघल गया", "the motors cable melted")
BATTERY_MELTS = ("the battery terminal near the motor melted", "motor side charging port melted",
                 "the charger plug by the hub melted", "मोटर के पास बैटरी का टर्मिनल पिघल गया",
                 "मोटर के पास टर्मिनल पिघल गया", "something is melted in battery", "connector melted", "E-06",
                 "terminal pighal gaya", "my EMotorad connector melted")


class MotorMeltTests(unittest.TestCase):
    """Ruling 3: a melt on a motor part is not a battery melt."""

    def test_the_motor_words(self):
        for text in MOTOR_NAMED:
            with self.subTest(text=text):
                self.assertTrue(melt_ask.names_motor(text))
        for text in NOT_MOTOR:
            with self.subTest(text=text):
                self.assertFalse(melt_ask.names_motor(text))

    def test_the_battery_words(self):
        for text in BATTERY_NAMED:
            with self.subTest(text=text):
                self.assertTrue(melt_ask.names_battery(text))
        for text in NOT_BATTERY:
            with self.subTest(text=text):
                self.assertFalse(melt_ask.names_battery(text))

    def test_the_patterns_use_neither_word_boundaries_nor_word_classes(self):
        for pattern in (melt_ask.MOTOR_WORDS, melt_ask.BATTERY_WORDS):
            with self.subTest(pattern=pattern.pattern):
                self.assertNotIn("\\b", pattern.pattern)
                self.assertNotIn("\\w", pattern.pattern)
                self.assertIn("(?<![a-z0-9])", pattern.pattern)

    def test_a_melt_naming_the_motor_and_no_battery_word_is_not_a_battery_melt(self):
        ask = active()
        for text in MOTOR_MELTS:
            with self.subTest(text=text):
                self.assertTrue(ask.triggered(text))
                self.assertFalse(ask.battery_melt(text))
        for text in BATTERY_MELTS:
            with self.subTest(text=text):
                self.assertTrue(ask.battery_melt(text))
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assertFalse(ask.battery_melt(text))

    def test_my_motor_connector_is_melted_gets_no_ask_and_triage_routes_it(self):
        chat = MeltChat([say(MOTOR_REPLY)])
        reply = chat.say("my motor connector is melted")
        self.assertEqual(reply.handled_by, "motor_support")
        self.assertIn(MOTOR_REPLY, reply.text)
        self.assertEqual(reply.attachments, [])
        self.assertEqual(chat.events("melt_ask"), [])
        self.assertEqual(chat.state().agent, "motor_support")
        self.assertFalse(chat.state().melt_pending)
        self.assertEqual(chat.state().melt_asked_frames, [])

    def test_a_motor_melt_on_a_routed_chat_gets_no_ask(self):
        chat = MeltChat([say(LATER_REPLY)], select=FRAME, routed="battery_support")
        reply = chat.say("मोटर का कनेक्टर पिघल गया")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(chat.events("melt_ask"), [])

    def test_the_battery_terminal_near_the_motor_gets_it(self):
        chat = MeltChat()
        reply = chat.say("the battery terminal near the motor melted")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertEqual(reply.text, first_reply(melt_ask.TEXT_EN))
        self.assertEqual(chat.state().melt_asked_frames, [FRAME])


class MeltIsTheBatteryTopicTests(unittest.TestCase):
    """Ruling 2: with the ask on, a battery melt is the battery topic in
    triage; with it off, triage is exactly as today."""

    def test_triage_reads_a_battery_melt_as_the_battery_topic_only_when_told(self):
        ask = active()
        on, off = TriageAgent(TOPIC_AGENTS, battery_melt=ask.battery_melt), TriageAgent(TOPIC_AGENTS)
        for text in ("connector melted", "E-06", "terminal pighal gaya", "the battery terminal near the motor melted"):
            with self.subTest(text=text):
                self.assertEqual(on.classify(text), "battery")
                self.assertIsNone(off.classify(text))
        # A motor melt stays motor; text with no melt is as classify_issue says.
        self.assertEqual(on.classify("my motor connector is melted"), "motor")
        self.assertEqual(on.classify("the motor is noisy"), "motor")
        self.assertIsNone(on.classify("hello there"))

    def test_with_the_ask_on_a_first_message_melt_is_routed_and_asked(self):
        for text, expected in (("connector melted", melt_ask.TEXT_EN), ("E-06", melt_ask.TEXT_EN),
                               ("terminal pighal gaya", melt_ask.TEXT_EN), ("टर्मिनल पिघल गया", melt_ask.TEXT_HI)):
            with self.subTest(text=text):
                chat = MeltChat()
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual(reply.handled_by, "melt_ask")
                self.assertEqual(reply.text, first_reply(expected))
                self.assertEqual(len(reply.attachments), 3)
                self.assertEqual(chat.state().agent, "battery_support")
                self.assertEqual(chat.state().melt_asked_frames, [FRAME])

    def test_with_two_bikes_a_melt_alone_is_asked_once_the_bike_is_chosen(self):
        chat = MeltChat(phone=TWO_BIKES)
        which = chat.say("connector melted")
        self.assertEqual((which.handled_by, which.metadata["reason"]), ("triage", "multiple_bikes:2"))
        self.assertTrue(chat.state().melt_pending)
        reply = chat.say("EMX Plus")
        self.assertEqual(chat.llm.requests, [])
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertEqual(reply.text, melt_ask.TEXT_EN)
        self.assertEqual(chat.state().melt_asked_frames, ["EMXP2026001234"])

    def test_on_the_web_chat_a_melt_alone_before_verifying_is_asked_once_the_bike_is_chosen(self):
        store = VerificationStore()
        registry = build_registry(verification=store, today=TODAY, account_finder=fixtures.find_account_by_order_code)
        llm = ScriptedClaude([])
        conversations = InMemoryConversationStore()
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
            log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=conversations,
            self_service_identity=True, phone_resolver=store.verified_phone, otp_verified_at=store.verified_on,
            verify_first=True, melt_ask=active(),
        )

        def send(text):
            return runtime.handle(InboundMessage(
                conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
                identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

        self.assertEqual(send("connector melted").handled_by, "verify_first:ask_number")
        send(TWO_BIKES[3:])
        self.assertEqual(send(store.pending_code("c1")).handled_by, "verify_first:verified")
        reply = send("EMX Plus")
        self.assertEqual(llm.requests, [])
        self.assertEqual(reply.handled_by, "melt_ask")
        self.assertEqual(conversations.peek("c1").melt_asked_frames, ["EMXP2026001234"])

    def test_with_the_ask_off_triage_is_as_today(self):
        for text in ("connector melted", "E-06", "terminal pighal gaya"):
            with self.subTest(text=text):
                chat = MeltChat(ask=False)
                reply = chat.say(text)
                self.assertEqual(chat.llm.requests, [])
                self.assertEqual((reply.handled_by, reply.metadata["reason"]), ("triage", "issue_unknown"))
                self.assertIn("What is happening with the bike?", reply.text)
                self.assertEqual(reply.attachments, [])
                self.assertIsNone(chat.state().agent)


class WaitingAskTests(unittest.TestCase):
    """Ruling 1: a waiting ask survives only a turn that ended for want of a
    bike (triage asking which bike) or of verification (the verify step)."""

    def test_a_handover_clears_it(self):
        chat = MeltChat([say(LATER_REPLY)], select=FRAME, routed="battery_support")
        first = chat.say("my battery connector is melted, connect me to a human")
        self.assertEqual(first.handled_by, "guardrail:human_handoff")
        self.assertFalse(chat.state().melt_pending)
        second = chat.say("ok thanks, when will they call?")
        self.assertEqual(second.handled_by, "battery_support")
        self.assertIn(LATER_REPLY, second.text)
        self.assertEqual(second.attachments, [])
        self.assertEqual(chat.events("melt_ask"), [])
        self.assertEqual(chat.state().melt_asked_frames, [])

    def test_the_erasure_flow_clears_it(self):
        chat = MeltChat([say(LATER_REPLY)], select=FRAME, routed="battery_support")
        self.assertEqual(chat.say("my battery connector is melted, delete my data").handled_by, "erasure:confirm")
        self.assertFalse(chat.state().melt_pending)
        self.assertEqual(chat.say("no").handled_by, "erasure:kept")
        reply = chat.say("ok thanks")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertEqual(chat.events("melt_ask"), [])

    def test_on_the_web_chat_the_erasure_offered_on_verifying_clears_it(self):
        store = VerificationStore()
        registry = build_registry(verification=store, today=TODAY, account_finder=fixtures.find_account_by_order_code)
        llm = ScriptedClaude([say(LATER_REPLY)])
        conversations = InMemoryConversationStore()
        log = EventLog(path=None)
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
            log=log, resolver=IdentityResolver(registry), conversations=conversations,
            self_service_identity=True, phone_resolver=store.verified_phone, otp_verified_at=store.verified_on,
            verify_first=True, melt_ask=active(),
        )

        def send(text):
            return runtime.handle(InboundMessage(
                conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
                identity=Identity(strength=ANONYMOUS, em_aid="aid-1")))

        self.assertEqual(send("my battery connector is melted, delete my data").handled_by,
                         "verify_first:ask_number")
        self.assertTrue(conversations.peek("c1").melt_pending)
        send(TWO_BIKES[3:])
        self.assertEqual(send(store.pending_code("c1")).handled_by, "verify_first:verified")
        self.assertFalse(conversations.peek("c1").melt_pending)
        self.assertEqual(send("no").handled_by, "erasure:kept")
        self.assertEqual(send("EMX Plus").handled_by, "battery_support")
        self.assertEqual([e for e in log.events if e["event"] == "melt_ask"], [])

    def test_the_which_bike_question_keeps_it(self):
        chat = MeltChat(phone=TWO_BIKES)
        chat.say("my battery terminal is melted")
        self.assertTrue(chat.state().melt_pending)
        # A reply that names no listed bike is asked again: still for want of a bike.
        again = chat.say("hmm")
        self.assertEqual(again.metadata["reason"], "selection_unmatched")
        self.assertTrue(chat.state().melt_pending)
        self.assertEqual(chat.say("EMX Plus").handled_by, "melt_ask")


class AfterTheAskTests(unittest.TestCase):
    """Ruling 4: the battery and narrow agents are told the customer was
    asked, while the ask went out for the chosen bike and no evidence passed."""

    def test_the_note_is_the_ruled_text(self):
        self.assertEqual(melt_ask.ASKED_NOTE, ASKED_NOTE)
        self.assertNotIn(chr(0x2014), ASKED_NOTE)

    def test_the_battery_agent_is_told_after_the_ask(self):
        chat = MeltChat([say(LATER_REPLY), say(LATER_REPLY)], select=FRAME, routed="battery_support")
        chat.say("something is melted in battery")
        self.assertEqual(chat.say("ok, sending them now").handled_by, "battery_support")
        self.assertIn(ASKED_NOTE, chat.llm.requests[0]["system"])
        # Every turn while nothing has passed, not only the next one.
        chat.say("did you get them?")
        self.assertIn(ASKED_NOTE, chat.llm.requests[1]["system"])

    def test_the_narrow_agent_is_told_after_the_ask(self):
        narrow = ScriptedClaude([say(LATER_REPLY)])
        jev = ScriptedJev([JevDecision(answers={Q_CATEGORY: choose("battery", 0.95),
                                                Q_SUB_CATEGORY: choose("battery-melted-terminal", 0.9)})])
        chat = MeltChat(jev=jev, narrow_llm=narrow, standard_responses=[])
        self.assertEqual(chat.say("my battery connector is melted").handled_by, "melt_ask")
        reply = chat.say("ok, sending them now")
        self.assertEqual(reply.handled_by, "narrow_support")
        self.assertEqual(chat.llm.requests, [])
        self.assertIn(ASKED_NOTE, narrow.requests[0]["system"])

    def test_no_note_without_an_ask(self):
        for ask in (True, False):
            with self.subTest(ask=ask):
                chat = MeltChat([say(LATER_REPLY)], ask=ask, select=FRAME, routed="battery_support")
                chat.say("my battery won't charge")
                self.assertNotIn(ASKED_NOTE, chat.llm.requests[0]["system"])
        chat = MeltChat([say(AGENT_REPLY)], ask=False, select=FRAME, routed="battery_support")
        chat.say("something is melted in battery")
        self.assertNotIn(ASKED_NOTE, chat.llm.requests[0]["system"])

    def test_no_note_once_evidence_passed(self):
        chat = MeltChat([say(LATER_REPLY)], select=FRAME, routed="battery_support")
        chat.say("something is melted in battery")
        state = chat.state()
        state.evidence_verdict = verdict_record(
            {"passed": True, "seen": "Melted pins on the battery terminal."}, at="2026-10-06T10:00:00+00:00",
            frame=FRAME, started_at=state.started_at, component="battery")
        chat.say("did you get them?")
        self.assertNotIn(ASKED_NOTE, chat.llm.requests[0]["system"])

    def test_no_note_for_another_bike(self):
        chat = MeltChat([say(LATER_REPLY)], phone=THREE_BIKES)
        chat.say("my battery terminal is melted")
        self.assertEqual(chat.say("T-Rex Air").handled_by, "melt_ask")
        self.assertEqual(chat.say("other bike").handled_by, "navigation:change_bike")
        reply = chat.say("EMX Plus")
        self.assertEqual(reply.handled_by, "battery_support")
        self.assertNotIn(ASKED_NOTE, chat.llm.requests[0]["system"])


class EvidenceCapTests(unittest.TestCase):
    """Ruling 5: the ask is one evidence ask, and none is sent at the cap."""

    def test_at_the_cap_the_ask_is_not_sent_and_the_hand_over_applies(self):
        chat = MeltChat([say(AGENT_REPLY)], select=FRAME, routed="battery_support")
        chat.state().evidence_asks = MAX_EVIDENCE_ASKS
        reply = chat.say("something is melted in battery")
        self.assertEqual(reply.handled_by, "guardrail:evidence_not_forthcoming")
        self.assertTrue(reply.escalated)
        self.assertEqual(reply.attachments, [])
        self.assertEqual(chat.events("melt_ask"), [])
        self.assertEqual(chat.state().melt_asked_frames, [])
        self.assertFalse(chat.state().melt_pending)

    def test_one_below_the_cap_the_ask_is_the_last_one(self):
        chat = MeltChat(select=FRAME, routed="battery_support")
        chat.state().evidence_asks = MAX_EVIDENCE_ASKS - 1
        self.assertEqual(chat.say("something is melted in battery").handled_by, "melt_ask")
        self.assertEqual(chat.state().evidence_asks, MAX_EVIDENCE_ASKS)


class StateTests(unittest.TestCase):
    def test_the_melt_fields_round_trip(self):
        state = ConversationState(conversation_id="c1", melt_pending=True, melt_asked_frames=["EMXP2025004417"])
        self.assertEqual(ConversationState.from_json(state.to_json()), state)
        self.assertEqual((ConversationState(conversation_id="c2").melt_pending,
                          ConversationState(conversation_id="c2").melt_asked_frames), (False, []))

    def test_forgetting_the_bike_clears_a_pending_ask_and_keeps_what_was_asked(self):
        state = ConversationState(conversation_id="c1", melt_pending=True, melt_asked_frames=["EMXP2025004417"])
        state.forget_bike()
        self.assertFalse(state.melt_pending)
        self.assertEqual(state.melt_asked_frames, ["EMXP2025004417"])

    def test_a_turn_that_loses_a_save_race_keeps_its_pending_ask_and_adds_its_bikes(self):
        self.assertIn("melt_pending", TURN_FACT_FIELDS)
        chat = MeltChat()
        fresh = chat.conversations.get("c1")
        fresh.melt_asked_frames = ["TREX2024881201"]
        chat.conversations.save(fresh)
        ours = ConversationState.from_json(fresh.to_json())
        ours.melt_asked_frames = ["TREX2024881201", "EMXP2025004990"]
        ours.melt_pending = True
        loaded = {name: getattr(fresh, name) for name in TURN_FACT_FIELDS}
        merged = chat.runtime._merge_onto_fresh(
            ours, [], Reply(conversation_id="c1", text="ok", handled_by="battery_support"), loaded=loaded)
        self.assertEqual(merged.melt_asked_frames, ["TREX2024881201", "EMXP2025004990"])
        self.assertTrue(merged.melt_pending)


if __name__ == "__main__":
    unittest.main()
