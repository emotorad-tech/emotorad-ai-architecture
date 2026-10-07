"""Confirming a serial read off the customer's photo (serial_confirm.py, 7 October 2026).

Each reading of the battery serial, the controller's S/N or the frame number
is put to the customer once. Yes confirms it; no asks them to type it, and
what they type replaces the reading. All of it is code: the model is never
called on a turn that answers, and never writes the question.
"""

import unittest
from datetime import date

from emotorad_ai import serial_ask, serial_confirm
from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.media import load_catalogue
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.serial_confirm import CONFIRM, TYPE, WHICH, answer, question, start
from emotorad_ai.serial_read import reading_doc
from emotorad_ai.tools.mocks import build_registry

TODAY = date(2026, 10, 7)
NOW = "2026-10-07T10:00:00+00:00"
PHONE = "+919876543210"  # Ananya, one EMX Plus EMXP2025004417 (fixtures)
FRAME = "EMXP2025004417"
NO_ASK = "Please check the wall socket works by plugging in another appliance."


def reading(part, serial, key, read_at="2026-10-07T09:00:00+00:00"):
    return reading_doc("c1", "cluster-1", FRAME, key,
                       {"part": part, "serial": serial, "legible": serial is not None, "seal": None},
                       "m", read_at)


BATTERY = reading("battery", "EMIN2407150123", "k-battery")
FRAME_READ = reading("frame", "EMXP2025004417", "k-frame")
CONTROLLER_UNREAD = reading("controller", None, "k-controller")
SEAL = reading_doc("c1", "cluster-1", FRAME, "k-seal",
                   {"part": "warranty_seal", "serial": None, "legible": False, "seal": "torn"}, "m",
                   "2026-10-07T09:00:00+00:00")


class StartTests(unittest.TestCase):
    def test_readable_ones_are_confirmed_in_part_order_and_unreadable_ones_typed(self):
        state = start([FRAME_READ, CONTROLLER_UNREAD, BATTERY, SEAL])
        self.assertEqual((state["step"], state["confirm"], state["typing"]),
                         (CONFIRM, ["k-battery", "k-frame"], ["k-controller"]))

    def test_the_seal_is_never_asked_about(self):
        self.assertIsNone(start([SEAL]))

    def test_only_unreadable_ones_start_by_typing(self):
        state = start([CONTROLLER_UNREAD])
        self.assertEqual(state["step"], TYPE)
        self.assertEqual(question(state, False),
                         "We couldn't read your controller serial number (S/N) from the photo. Please type it "
                         "exactly as it is printed.")


class QuestionTests(unittest.TestCase):
    def test_one_reading(self):
        self.assertEqual(question(start([BATTERY]), False),
                         "We read your battery serial number from your photo as EMIN2407150123. Is that right? "
                         "Please reply YES or NO.")

    def test_several_are_numbered(self):
        text = question(start([BATTERY, FRAME_READ]), False)
        self.assertIn("\n1. Battery serial number: EMIN2407150123\n2. Frame number: EMXP2025004417\n", text)
        self.assertTrue(text.endswith("Reply YES, or the number of any that is wrong."))

    def test_hindi(self):
        self.assertIn("EMIN2407150123", question(start([BATTERY]), True))
        self.assertIn("हाँ या नहीं", question(start([BATTERY]), True))


class AnswerTests(unittest.TestCase):
    def test_yes_confirms_every_one(self):
        for said in ("yes", "Yes!", "haan sahi hai", "हाँ", "correct"):
            with self.subTest(said=said):
                outcome = answer(start([BATTERY, FRAME_READ]), said, NOW)
                self.assertIsNone(outcome.confirm)
                self.assertEqual(outcome.reply, "Thank you, I've noted them." if said != "हाँ"
                                 else "धन्यवाद, मैंने इन्हें नोट कर लिया है।")
                self.assertEqual([i for i, _ in outcome.updates], ["k-battery", "k-frame"])
                self.assertEqual(outcome.updates[0][1], {"confirmed": True, "confirmed_at": NOW,
                                                         "confirmed_by": "customer"})

    def test_no_on_one_asks_for_it_typed_and_the_typed_one_replaces_the_reading(self):
        outcome = answer(start([BATTERY]), "no", NOW)
        self.assertEqual((outcome.confirm["step"], outcome.updates), (TYPE, []))
        self.assertEqual(outcome.reply, "Please type your battery serial number exactly as it is printed.")
        typed = answer(outcome.confirm, "emin 2407 1501 29", NOW)
        self.assertIsNone(typed.confirm)
        self.assertEqual(typed.reply, "Thank you, I've noted it.")
        self.assertEqual(typed.updates, [("k-battery", {"serial": "EMIN240715012" + "9", "ocr_serial": "EMIN2407150123",
                                                         "source": "typed", "confirmed": True, "confirmed_at": NOW,
                                                         "confirmed_by": "customer"})])

    def test_no_on_several_asks_which_and_the_others_are_confirmed(self):
        which = answer(start([BATTERY, FRAME_READ]), "nahi", NOW)
        self.assertEqual((which.confirm["step"], which.reply),
                         (WHICH, "Which one is wrong? Please reply with its number, for example 1."))
        named = answer(which.confirm, "2", NOW)
        self.assertEqual([i for i, _ in named.updates], ["k-battery"])
        self.assertEqual((named.confirm["step"], named.confirm["typing"]), (TYPE, ["k-frame"]))
        self.assertEqual(named.reply, "Please type your frame number exactly as it is printed.")

    def test_the_number_of_a_wrong_one_straight_away(self):
        named = answer(start([BATTERY, FRAME_READ]), "1 is wrong", NOW)
        self.assertEqual([i for i, _ in named.updates], ["k-frame"])
        self.assertEqual(named.confirm["typing"], ["k-battery"])

    def test_wrong_ones_are_typed_before_unreadable_ones(self):
        outcome = answer(start([BATTERY, CONTROLLER_UNREAD]), "no", NOW)
        self.assertEqual(outcome.confirm["typing"], ["k-battery", "k-controller"])
        after = answer(outcome.confirm, "EMIN2407150124", NOW)
        self.assertEqual(after.reply, "We couldn't read your controller serial number (S/N) from the photo. Please "
                                      "type it exactly as it is printed.")

    def test_a_message_that_is_not_an_answer_goes_on(self):
        for said in ("why do you need all these photos of my bike, it is new", "", "5", "ok", "ok what next",
                     "theek hai"):
            with self.subTest(said=said):
                outcome = answer(start([BATTERY, FRAME_READ]), said, NOW)
                self.assertIsNone(outcome.reply)
                self.assertEqual(outcome.updates, [])

    def test_something_that_is_not_a_serial_is_asked_again_then_left(self):
        typing = answer(start([BATTERY]), "no", NOW).confirm
        again = answer(typing, "abc", NOW)
        self.assertIn("That doesn't look like a battery serial number", again.reply)
        given_up = answer(again.confirm, "x!", NOW)
        self.assertEqual((given_up.confirm, given_up.updates, given_up.event), (None, [], "typing_given_up"))

    def test_skipping_one_leaves_it_unconfirmed(self):
        typing = answer(start([BATTERY]), "no", NOW).confirm
        skipped = answer(typing, "pata nahi", NOW)
        self.assertEqual((skipped.confirm, skipped.updates, skipped.event), (None, [], "typing_skipped"))


class Chat:
    def __init__(self, replies, readings=(), agent="battery_support"):
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        registry = build_registry(today=TODAY)
        ask, _ = serial_ask.from_env(load_catalogue(), _Signs(), {"EMOTORAD_SERIAL_ASK": "on"})
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=self.llm, log=self.log,
            resolver=IdentityResolver(registry), conversations=self.conversations, serial_ask=ask,
        )
        state = self.conversations.get("c1")
        state.select_bike(FRAME, "EMX Plus")
        state.route_to(agent)
        state.serials_asked_frames.append(FRAME)
        for doc in readings:
            self.conversations.add_serial_reading(doc)

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=VERIFIED, phone=PHONE, em_aid="aid-1"),
        ))

    def stored(self):
        return {r["_id"]: r for r in self.conversations.serial_readings_of("c1")}

    def state(self):
        return self.conversations.peek("c1")


class _Signs:
    def presign_get(self, key):
        return "https://signed.test/" + key


class ConversationTests(unittest.TestCase):
    def test_the_question_follows_the_agents_reply_and_yes_is_answered_by_code(self):
        chat = Chat([say(NO_ASK)], readings=[BATTERY])
        first = chat.say("what should I check?")
        self.assertTrue(first.text.endswith(question(start([BATTERY]), False)), first.text)
        self.assertEqual(chat.state().serial_asked_ids, ["k-battery"])
        second = chat.say("yes")
        # The model was called for the first turn only.
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertEqual((second.text, second.handled_by), ("Thank you, I've noted it.", "serial_confirm"))
        self.assertTrue(chat.stored()["k-battery"]["confirmed"])
        self.assertIsNone(chat.state().serial_confirm)

    def test_no_then_the_typed_serial_is_kept_and_the_model_never_runs(self):
        chat = Chat([say(NO_ASK)], readings=[FRAME_READ])
        chat.say("what next?")
        self.assertEqual(chat.say("no").text, "Please type your frame number exactly as it is printed.")
        self.assertEqual(chat.say("EMXP2025004418").text, "Thank you, I've noted it.")
        self.assertEqual(len(chat.llm.requests), 1)
        stored = chat.stored()["k-frame"]
        self.assertEqual((stored["serial"], stored["ocr_serial"], stored["source"], stored["confirmed"]),
                         ("EMXP2025004418", "EMXP2025004417", "typed", True))

    def test_a_message_that_is_not_an_answer_reaches_the_agent_and_the_question_is_asked_once_more(self):
        chat = Chat([say(NO_ASK), say("Try a different socket."), say("Then try the charger cable.")],
                    readings=[BATTERY])
        chat.say("what should I check?")
        again = chat.say("I already tried that socket, what else could be wrong with the charger?")
        self.assertEqual(len(chat.llm.requests), 2)
        self.assertTrue(again.text.endswith(question(start([BATTERY]), False)), again.text)
        dropped = chat.say("still no luck with the charger cable either, anything else to try?")
        self.assertNotIn("Is that right?", dropped.text)
        self.assertIsNone(chat.state().serial_confirm)
        self.assertFalse(chat.stored()["k-battery"]["confirmed"])

    def test_each_reading_is_put_to_the_customer_once(self):
        chat = Chat([say(NO_ASK), say(NO_ASK)], readings=[BATTERY])
        chat.say("what should I check?")
        chat.say("yes")
        later = chat.say("ok and what about the lights?")
        self.assertNotIn("Is that right?", later.text)

    def test_safety_wins_over_a_confirmation(self):
        chat = Chat([say(NO_ASK)], readings=[BATTERY])
        chat.say("what should I check?")
        reply = chat.say("the battery is smoking")
        self.assertTrue(reply.handled_by.startswith("guardrail:"), reply.handled_by)
        self.assertEqual(len(chat.llm.requests), 1)

    def test_switched_off_nothing_is_asked(self):
        chat = Chat([say(NO_ASK)], readings=[BATTERY])
        chat.runtime.serial_ask = None
        reply = chat.say("what should I check?")
        self.assertNotIn("Is that right?", reply.text)

    def test_the_log_never_carries_a_value(self):
        chat = Chat([say(NO_ASK)], readings=[BATTERY])
        chat.say("what should I check?")
        chat.say("no")
        chat.say("EMIN2407150999")
        logged = str([e for e in chat.log.events if e["event"].startswith("serial_confirm")])
        self.assertNotIn("EMIN", logged)


if __name__ == "__main__":
    unittest.main()
