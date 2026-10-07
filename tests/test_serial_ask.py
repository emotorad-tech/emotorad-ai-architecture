"""The serial-photo ask (serial_ask.py, the person's rules of 7 October 2026).

When a fault agent asks for the customer's media, code adds a request for
the frame number sticker and, for a battery issue, the battery's serial
sticker, the controller's label and the battery's warranty seal, once per
bike, with an example of each from the reference library. The model never
writes it and cannot leave it out.
"""

import unittest
from datetime import date

from emotorad_ai import serial_ask
from emotorad_ai.config import Settings
from emotorad_ai.contract import VERIFIED, Identity, InboundMessage
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, say
from emotorad_ai.media import load_catalogue
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import build_registry

TODAY = date(2026, 10, 7)
ONE_BIKE = "+919876543210"  # Ananya, one EMX Plus EMXP2025004417 (fixtures)
DOODLE = "+919812345678"  # Rohit, one Doodle V3 DDL32022119302 (fixtures)
ON = {"EMOTORAD_SERIAL_ASK": "on"}
ASKS_VIDEO = "Thanks. Could you send a short video of the charger plugged in?"
ASKS_VIDEO_HI = "धन्यवाद। क्या आप चार्जर लगे हुए का एक छोटा वीडियो भेज सकते हैं?"
NO_ASK = "Please check the wall socket works by plugging in another appliance."

BATTERY_URL = "https://signed.test/assets/library/battery/photos/serial-label-downtube.w900.webp"
DOODLE_URL = "https://signed.test/assets/library/battery/photos/serial-label-doodle.w900.webp"
CONTROLLER_URL = "https://signed.test/assets/library/controller/photos/serial-label.w900.webp"
SEAL_URL = "https://signed.test/assets/library/battery/photos/warranty-seal-intact.w900.webp"
FRAME_URL = "https://signed.test/assets/library/frame/photos/frame-number-sticker.w900.webp"
ALL_BATTERY = ["battery", "controller", "warranty_seal", "frame"]


class FakeStore:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def presign_get(self, key):
        if any(part in key for part in self.fail):
            raise OSError("cannot sign %s" % key)
        return "https://signed.test/" + key


def active(store=None):
    ask, status = serial_ask.from_env(load_catalogue(), store or FakeStore(), ON)
    assert ask is not None, status
    return ask


class Chat:
    def __init__(self, replies, phone=ONE_BIKE, frame="EMXP2025004417", label="EMX Plus", agent="battery_support",
                 ask=True, store=None):
        self.llm = ScriptedClaude(list(replies))
        self.conversations = InMemoryConversationStore()
        self.log = EventLog(path=None)
        self.store = store or FakeStore()
        self.phone = phone
        registry = build_registry(today=TODAY)
        self.runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=self.llm, log=self.log,
            resolver=IdentityResolver(registry), conversations=self.conversations,
            serial_ask=active(self.store) if ask else None,
        )
        state = self.conversations.get("c1")
        state.select_bike(frame, label)
        state.route_to(agent)

    def say(self, text):
        return self.runtime.handle(InboundMessage(
            conversation_id="c1", persona="customer", channel="website_chat", message_text=text,
            identity=Identity(strength=VERIFIED, phone=self.phone, em_aid="aid-1"),
        ))

    def state(self):
        return self.conversations.peek("c1")

    def events(self, name):
        return [e for e in self.log.events if e["event"] == name]


class SwitchTests(unittest.TestCase):
    def test_off_unless_exactly_on(self):
        for value in ("", "ON", "yes", "1"):
            self.assertEqual(serial_ask.from_env(load_catalogue(), FakeStore(), {"EMOTORAD_SERIAL_ASK": value}),
                             (None, "off"))

    def test_on_with_the_library_pictures(self):
        ask, status = serial_ask.from_env(load_catalogue(), FakeStore(), ON)
        self.assertEqual(status, "on")
        self.assertEqual(ask.items[serial_ask.BATTERY_KEY]["id"], "library/battery/photos/serial-label-downtube.jpg")
        self.assertEqual(ask.items[serial_ask.CONTROLLER_KEY]["id"], "library/controller/photos/serial-label.jpg")
        self.assertEqual(ask.items[serial_ask.SEAL_KEY]["id"], "library/battery/photos/warranty-seal-intact.jpg")
        self.assertEqual(ask.items[serial_ask.FRAME_KEY]["id"], "library/frame/photos/frame-number-sticker.jpg")

    def test_a_missing_or_offered_picture_keeps_it_off_and_says_why(self):
        catalogue = load_catalogue()
        del catalogue[serial_ask.CONTROLLER_KEY]
        self.assertEqual(serial_ask.from_env(catalogue, FakeStore(), ON), (None, "off: missing controller_serial_label"))
        catalogue = load_catalogue()
        catalogue[serial_ask.BATTERY_KEY] = dict(catalogue[serial_ask.BATTERY_KEY], code_only=False)
        self.assertEqual(serial_ask.from_env(catalogue, FakeStore(), ON), (None, "off: not code_only battery_serial_label"))


class PartsTests(unittest.TestCase):
    def test_a_battery_issue_asks_for_all_four(self):
        self.assertEqual(serial_ask.parts_for(True, "EMX Plus"), ALL_BATTERY)

    def test_a_doodle_has_no_seal_ask(self):
        self.assertEqual(serial_ask.parts_for(True, "Doodle V3"), ["battery", "controller", "frame"])

    def test_after_the_melt_ask_the_battery_and_controller_are_not_asked_again(self):
        self.assertEqual(serial_ask.parts_for(True, "EMX Plus", melt_asked=True), ["warranty_seal", "frame"])

    def test_every_other_issue_asks_for_the_frame_number(self):
        self.assertEqual(serial_ask.parts_for(False, "EMX Plus"), ["frame"])

    def test_the_text_lists_several_and_names_one(self):
        several = serial_ask.text_for(ALL_BATTERY, False)
        self.assertIn("four photos", several)
        self.assertIn("\n1. the serial number sticker on your battery", several)
        self.assertIn("\n4. your bike's frame number sticker", several)
        one = serial_ask.text_for(["frame"], False)
        self.assertTrue(one.startswith("Along with that, please send a photo of your bike's frame number sticker"))
        self.assertIn("फ़्रेम नंबर", serial_ask.text_for(["frame"], True))


class AskTests(unittest.TestCase):
    def test_the_first_media_ask_gets_the_serial_request_and_every_example(self):
        chat = Chat([say(ASKS_VIDEO)])
        reply = chat.say("my battery won't charge")
        text = serial_ask.text_for(ALL_BATTERY, False)
        # After the bot's first-reply disclosure line, its own ask, then ours.
        self.assertIn(ASKS_VIDEO + "\n\n" + text, reply.text)
        self.assertTrue(reply.text.endswith(text), reply.text)
        self.assertEqual([a.url for a in reply.attachments], [BATTERY_URL, CONTROLLER_URL, SEAL_URL, FRAME_URL])
        self.assertEqual(chat.state().serials_asked_frames, ["EMXP2025004417"])
        # The model wrote none of it, and the next turn sees it as the bot's own words.
        self.assertEqual(len(chat.llm.requests), 1)
        self.assertIn(text, chat.state().history[-1]["content"][0]["text"])
        (event,) = chat.events("serial_ask")
        self.assertEqual((event["parts"], event["pictures"], event["language"]), (ALL_BATTERY, 4, "en"))

    def test_it_goes_once_per_bike(self):
        chat = Chat([say(ASKS_VIDEO), say(ASKS_VIDEO)])
        chat.say("my battery won't charge")
        second = chat.say("here")
        self.assertNotIn("Along with that", second.text)
        self.assertEqual(second.attachments, [])
        self.assertEqual(chat.state().serials_asked_frames, ["EMXP2025004417"])

    def test_a_doodle_owner_is_shown_the_doodle_battery_sticker_and_no_seal(self):
        chat = Chat([say(ASKS_VIDEO)], phone=DOODLE, frame="DDL32022119302", label="Doodle V3")
        reply = chat.say("battery not charging")
        self.assertEqual([a.url for a in reply.attachments], [DOODLE_URL, CONTROLLER_URL, FRAME_URL])

    def test_a_reply_that_asks_for_nothing_gets_nothing(self):
        chat = Chat([say(NO_ASK)])
        reply = chat.say("my battery won't charge")
        self.assertTrue(reply.text.endswith(NO_ASK), reply.text)
        self.assertEqual(chat.state().serials_asked_frames, [])

    def test_a_hindi_reply_gets_the_hindi_request(self):
        chat = Chat([say(ASKS_VIDEO_HI)])
        reply = chat.say("बैटरी चार्ज नहीं हो रही")
        self.assertTrue(reply.text.endswith(serial_ask.text_for(ALL_BATTERY, True)), reply.text)

    def test_after_the_melt_ask_only_the_seal_and_the_frame_are_asked(self):
        chat = Chat([say(ASKS_VIDEO)])
        chat.conversations.get("c1").melt_asked_frames.append("EMXP2025004417")
        reply = chat.say("here is the video")
        self.assertTrue(reply.text.endswith(serial_ask.text_for(["warranty_seal", "frame"], False)), reply.text)
        self.assertEqual([a.url for a in reply.attachments], [SEAL_URL, FRAME_URL])

    def test_a_motor_chat_is_asked_for_the_frame_number_only(self):
        chat = Chat([say(ASKS_VIDEO)], agent="motor_support")
        reply = chat.say("my motor makes a noise")
        self.assertTrue(reply.text.endswith(serial_ask.text_for(["frame"], False)), reply.text)
        self.assertEqual([a.url for a in reply.attachments], [FRAME_URL])
        self.assertEqual(chat.state().serials_asked_frames, ["EMXP2025004417"])

    def test_switched_off_nothing_is_added(self):
        chat = Chat([say(ASKS_VIDEO)], ask=False)
        reply = chat.say("my battery won't charge")
        self.assertNotIn("Along with that", reply.text)
        self.assertEqual(reply.attachments, [])

    def test_a_picture_that_will_not_sign_is_left_out_and_named(self):
        chat = Chat([say(ASKS_VIDEO)], store=FakeStore(fail=("controller/photos/serial-label",)))
        reply = chat.say("my battery won't charge")
        self.assertTrue(reply.text.endswith(serial_ask.text_for(ALL_BATTERY, False)))
        self.assertEqual([a.url for a in reply.attachments], [BATTERY_URL, SEAL_URL, FRAME_URL])
        (missing,) = chat.events("serial_ask_media_missing")
        self.assertEqual(missing["keys"], ["controller_serial_label"])


if __name__ == "__main__":
    unittest.main()
