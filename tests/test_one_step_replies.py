"""One step per reply (the person's decision, 2026-09-29).

A customer should get one thing at a time: one step, or one question, in a few
short sentences, and the next step once they have answered. Two parts:

- The rule, appended by the agent loop to every customer agent's system
  prompt, so a promoted prompt cannot lose it. The dealer agent is exempt: an
  order summary has to list every line.
- A backstop in code. A reply over the limit is sent once to the same model to
  cut it to its first step or question. The cut is used only if every
  reference in the original survives and it is within the limit; otherwise
  the original goes, logged. The history holds what the customer was sent.
"""

import unittest
from datetime import date

from emotorad_ai.adapters import DealerWhatsAppAdapter, WebsiteChatAdapter
from emotorad_ai.config import Settings
from emotorad_ai.conversation import InMemoryConversationStore
from emotorad_ai.decisions import Q_CATEGORY, Q_SUB_CATEGORY
from emotorad_ai.guardrails import COVERAGE_BLOCKED_MESSAGE
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.jev import JevDecision, ScriptedJev, choose
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.one_step import MAX_SENTENCES, MAX_WORDS, ONE_STEP_RULE, is_too_long, references
from emotorad_ai.runtime import Runtime
from emotorad_ai.tools.mocks import CREATE_SUPPORT_TICKET, SEARCH_BATTERY_KNOWLEDGE, build_registry

TODAY = date(2026, 7, 28)
LONG = (
    "Let us work through this together. First, check that the wall socket works by plugging in another "
    "appliance. Second, look at the charger light: it should turn red when connected. Third, make sure the "
    "battery key is in the on position. Fourth, leave it on charge for at least two hours before trying "
    "again. Fifth, if none of that helps, try a different socket in another room. Sixth, tell me what you see."
)
SHORT = "First, please check the wall socket works by plugging in another appliance. Does it?"
TICKET = {"category": "battery_charging", "severity": "normal", "description": "Dead after the steps.",
          "idempotency_key": "k1"}


def runtime(replies, store=None, **kwargs):
    registry = build_registry(today=TODAY)
    store = store or InMemoryConversationStore()
    rt = Runtime(settings=Settings(log_path=""), registry=registry, llm=ScriptedClaude(list(replies)),
                 log=EventLog(path=None), resolver=IdentityResolver(registry), conversations=store, **kwargs)
    return rt


def routed(rt):
    rt.conversations.get("c1").route_to("battery_support")
    return rt


def send(rt, text, attachments=()):
    return rt.handle(WebsiteChatAdapter(rt.resolver).to_message({
        "conversation_id": "c1", "session_token": "sess-ananya", "text": text, "attachments": list(attachments)}))


def texts_in(history):
    out = []
    for entry in history:
        content = entry["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(b.get("text", "") for b in content if b.get("type") == "text")
    return out


def events(rt, name):
    return [e for e in rt.log.events if e["event"] == name]


class MeasureTests(unittest.TestCase):
    def test_the_limits(self):
        self.assertEqual((MAX_WORDS, MAX_SENTENCES), (80, 4))
        self.assertTrue(is_too_long(LONG))
        self.assertFalse(is_too_long(SHORT))
        self.assertTrue(is_too_long(" ".join(["word"] * (MAX_WORDS + 1))))
        self.assertFalse(is_too_long(" ".join(["word"] * MAX_WORDS)))
        self.assertTrue(is_too_long("One. Two. Three. Four. Five."))
        self.assertFalse(is_too_long("One. Two. Three. Four."))

    def test_a_reference_inside_a_sentence_is_not_a_sentence_break(self):
        self.assertFalse(is_too_long("Your ticket is EM-00001. Visit on 1.8.2026 at 10.30 a.m. is booked. Anything else?"))

    def test_references_are_the_ids_the_tools_mint(self):
        self.assertEqual(references("Ticket EM-00001, booking BK-00002 and order RO-00003."),
                         {"EM-00001", "BK-00002", "RO-00003"})


class RuleTests(unittest.TestCase):
    def test_every_customer_agent_is_given_the_rule_last(self):
        rt = routed(runtime([say(SHORT)]))
        send(rt, "my battery won't charge")
        self.assertTrue(rt.llm.requests[0]["system"].endswith(ONE_STEP_RULE))

    def test_the_narrow_agent_too(self):
        narrow = ScriptedClaude([say(SHORT)])
        rt = runtime([], narrow_llm=narrow, jev=ScriptedJev([JevDecision(answers={
            Q_CATEGORY: choose("battery", 0.95), Q_SUB_CATEGORY: choose("battery-wont-charge", 0.9)})]),
            standard_responses=[])
        answer = send(rt, "my battery won't charge")
        self.assertEqual(answer.handled_by, "narrow_support")
        self.assertTrue(narrow.requests[0]["system"].endswith(ONE_STEP_RULE))

    def test_the_dealer_agent_is_not(self):
        rt = runtime([say(LONG)])
        rt.handle(DealerWhatsAppAdapter(rt.resolver).to_message(
            {"from": "919000000001", "text": "I need 5 EMX Plus", "conversation_id": "d1"}))
        self.assertNotIn(ONE_STEP_RULE, rt.llm.requests[0]["system"])
        self.assertEqual(len(rt.llm.requests), 1)  # and its long reply is not cut


class BackstopTests(unittest.TestCase):
    def test_a_long_reply_is_cut_to_its_first_step(self):
        rt = routed(runtime([say(LONG), say(SHORT)]))
        answer = send(rt, "my battery won't charge")
        self.assertIn(SHORT, answer.text)
        self.assertNotIn("Sixth", answer.text)
        [event] = events(rt, "reply_shortened")
        self.assertEqual(event["agent"], "battery_support")
        self.assertGreater(event["sentences_before"], MAX_SENTENCES)
        self.assertLessEqual(event["sentences_after"], MAX_SENTENCES)
        self.assertLessEqual(event["words_after"], MAX_WORDS)
        self.assertNotIn("text", event)  # counts only; the text is in the transcript

    def test_the_history_holds_what_the_customer_was_sent(self):
        rt = routed(runtime([say(LONG), say(SHORT)]))
        send(rt, "my battery won't charge")
        said = texts_in(rt.conversations.peek("c1").history)
        self.assertIn(SHORT, said)
        self.assertFalse(any("Sixth" in t for t in said))

    def test_the_cut_is_asked_of_the_same_model_with_no_tools_and_is_costed(self):
        rt = routed(runtime([say(LONG), say(SHORT)]))
        send(rt, "my battery won't charge")
        cut = rt.llm.requests[1]
        self.assertEqual(cut["tools"], [])
        self.assertIn(LONG, str(cut["messages"]))
        self.assertTrue([e for e in events(rt, "llm_turn") if e["agent"] == "one_step"])

    def test_a_short_reply_costs_no_second_call(self):
        rt = routed(runtime([say(SHORT)]))
        send(rt, "my battery won't charge")
        self.assertEqual(len(rt.llm.requests), 1)
        self.assertEqual(events(rt, "reply_shortened") + events(rt, "reply_too_long"), [])

    def test_a_cut_that_loses_a_reference_is_not_used(self):
        store = InMemoryConversationStore()
        store.get("c1").route_to("battery_support")
        store.get("c1").evidence_seen = True  # a photo earlier: the fault ticket may be raised
        long_with_ref = LONG + " Your ticket is EM-00001."
        rt = runtime([call_tool(CREATE_SUPPORT_TICKET, dict(TICKET), "toolu_1"), say(long_with_ref), say(SHORT)],
                     store=store)
        answer = send(rt, "still dead, please raise a complaint")
        self.assertIn("EM-00001", answer.text)
        self.assertIn("Sixth", answer.text)
        [event] = events(rt, "reply_too_long")
        self.assertEqual(event["reason"], "reference_lost")

    def test_a_cut_that_is_still_too_long_is_not_used(self):
        rt = routed(runtime([say(LONG), say(LONG + " Also.")]))
        answer = send(rt, "my battery won't charge")
        self.assertIn("Sixth", answer.text)
        self.assertEqual(events(rt, "reply_too_long")[0]["reason"], "still_too_long")

    def test_a_failed_cut_sends_the_original_and_says_why(self):
        rt = routed(runtime([say(LONG)]))  # nothing queued for the cut: the call raises
        answer = send(rt, "my battery won't charge")
        self.assertIn("Sixth", answer.text)
        [event] = events(rt, "reply_too_long")
        self.assertEqual(event["reason"], "shorten_failed")
        self.assertEqual(event["error"], "AssertionError")

    def test_a_cut_that_answers_with_a_tool_call_is_not_used(self):
        rt = routed(runtime([say(LONG), call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "x"}, "toolu_9")]))
        answer = send(rt, "my battery won't charge")
        self.assertIn("Sixth", answer.text)
        self.assertEqual(events(rt, "reply_too_long")[0]["reason"], "no_text")

    def test_the_post_checks_judge_the_text_that_is_sent(self):
        claim = "Good news: this is covered under warranty, so the replacement is free."
        rt = routed(runtime([say(LONG), say(claim)]))
        answer = send(rt, "my battery won't charge")
        self.assertEqual(answer.handled_by, "guardrail:coverage_post_check")
        self.assertIn(COVERAGE_BLOCKED_MESSAGE, answer.text)

    def test_a_turn_with_tool_calls_keeps_its_tool_pairs_and_only_the_cut_text(self):
        rt = routed(runtime([
            call_tool(SEARCH_BATTERY_KNOWLEDGE, {"query": "won't charge"}, "toolu_1"),
            say(LONG), say(SHORT),
        ]))
        send(rt, "my battery won't charge")
        history = rt.conversations.peek("c1").history
        tool_uses = [b for e in history if e["role"] == "assistant" and isinstance(e["content"], list)
                     for b in e["content"] if b.get("type") == "tool_use"]
        tool_results = [b for e in history if e["role"] == "user" and isinstance(e["content"], list)
                        for b in e["content"] if b.get("type") == "tool_result"]
        self.assertEqual([b["id"] for b in tool_uses], [b["tool_use_id"] for b in tool_results])
        self.assertEqual([t for t in texts_in(history) if t and "battery won't charge" not in t][-1], SHORT)

    def test_a_reply_written_by_code_is_never_cut(self):
        rt = runtime([])
        answer = send(rt, "my battery is swollen and smoking")
        self.assertEqual(answer.handled_by, "guardrail:battery_safety")
        self.assertEqual(rt.llm.requests, [])


# Long filler in each language, so a reply built from a caution and one of
# these runs past the limits and the backstop would cut it.
HINGLISH_STEPS = (
    " Pehle wall socket check kijiye. Phir charger ki light dekhiye. Phir battery ki chaabi on kijiye."
    " Phir do ghante rukiye. Phir mujhe bataiye ki kya dikha."
)
HINDI_STEPS = (
    " पहले दीवार का सॉकेट जाँचिए। फिर चार्जर की लाइट देखिए। फिर बैटरी की चाबी देखिए।"
    " फिर दो घंटे रुकिए। फिर मुझे बताइए कि क्या दिखा।"
)
# The final review's probe (2026-09-29): this was cut to the last question
# alone, and the customer lost the caution.
PROBE = (
    "Please don't charge the battery again until our team has looked at it, and keep it away from anything "
    "that can burn. A warm pack after a long ride can be normal, but one that smells hot is not. "
    "Could you tell me when this started? Also, how old is the charger you are using? "
    "And does the charger light turn green at any point? Finally, please send a photo of the battery label."
)


class CautionIsNeverCutTests(unittest.TestCase):
    """A reply that warns the customer off something goes out whole, in every
    language the bot answers in. A cut that keeps the question and drops the
    caution is worse than a long reply."""

    def assert_sent_whole(self, reply, customer="the battery feels a bit warm after charging"):
        self.assertTrue(is_too_long(reply), "the test reply must be long enough to be cut")
        rt = routed(runtime([say(reply), say(SHORT)]))
        answer = send(rt, customer)
        self.assertEqual(len(rt.llm.requests), 1)  # no second call: nothing asked to cut it
        self.assertIn(reply, answer.text)
        self.assertNotIn(SHORT, answer.text)
        self.assertEqual(events(rt, "reply_shortened") + events(rt, "reply_too_long"), [])

    def test_the_final_reviews_probe(self):
        self.assert_sent_whole(PROBE)

    def test_english(self):
        self.assert_sent_whole("Please don't charge it tonight. " + LONG)

    def test_english_hazard_words_the_safety_gate_uses(self):
        self.assert_sent_whole("If you see any smoke, move away from the bike. " + LONG)

    def test_hinglish(self):
        self.assert_sent_whole("Abhi charging band kar dijiye." + HINGLISH_STEPS, "battery garam lag rahi hai")

    def test_hinglish_hazard_words_the_safety_gate_uses(self):
        self.assert_sent_whole("Agar battery se dhuan nikle to usse door rahiye." + HINGLISH_STEPS,
                               "battery garam lag rahi hai")

    def test_hindi(self):
        self.assert_sent_whole("कृपया अभी बैटरी चार्ज न करें।" + HINDI_STEPS, "बैटरी गरम लग रही है")


class CarriesCautionTests(unittest.TestCase):
    """The rule the backstop asks before it cuts (guardrails.carries_caution)."""

    CAUTIONS = (
        # English: don't / do not / never / stop, with what not to do.
        "Please don't charge it tonight.",
        "Please don’t charge it tonight.",  # the curly apostrophe models write
        "Stop riding the bike until it has been checked.",
        "Do not force the charger into the port.",
        "Never open the battery case yourself.",
        "Please stop using it for now.",
        "Don't touch the terminals.",
        "Switch it off at the key.",
        "Turn the bike off and wait.",
        # Hinglish.
        "Battery charge mat karo.",
        "Abhi charging band kar dijiye.",
        "Bike abhi mat chalaiye.",
        "Charger ko use na karein.",
        # Hindi.
        "चार्ज न करें।",
        "कृपया बैटरी चार्ज मत करें।",
        "बाइक मत चलाइए।",
        "चार्जिंग बंद कर दीजिए।",
        # The safety gate's own words, English and romanised Hindi.
        "If it is swollen, keep it outside.",
        "If you see smoke, move away.",
        "Agar dhuan dikhe to door rahiye.",
        "If the brakes are not working, do not ride.",
        # What the evidence check already treated as a safety handover.
        "Our safety team will call you.",
    )
    ORDINARY = (
        LONG,
        SHORT,
        "Don't worry, this is common after a long ride.",
        "Kya battery charge nahi ho rahi? Charger ki light dekhiye.",
        "Kya charger ki light jalti hai?",
        "क्या चार्जर की लाइट जलती है?",
    )

    def test_a_caution_is_recognised(self):
        from emotorad_ai.guardrails import carries_caution

        for text in self.CAUTIONS:
            with self.subTest(text=text):
                self.assertTrue(carries_caution(text))

    def test_an_ordinary_step_is_not(self):
        from emotorad_ai.guardrails import carries_caution

        for text in self.ORDINARY:
            with self.subTest(text=text):
                self.assertFalse(carries_caution(text))

    def test_the_cut_is_told_to_keep_every_caution_and_every_cost_word_for_word(self):
        from emotorad_ai.one_step import SHORTEN_SYSTEM

        self.assertIn("Keep every warning or caution, and every statement of cost or charges, word for word.",
                      SHORTEN_SYSTEM)


class LiveEvalCheckTests(unittest.TestCase):
    """A paid run shows every reply the backstop could not cut."""

    def failures(self, events):
        from emotorad_ai.contract import Reply
        from emotorad_ai.live_eval.checks import TurnRecord, check_turn
        from emotorad_ai.live_eval.scenarios import Expect, Who

        reply = Reply(conversation_id="c", text="Hi, I'm EMotorad's virtual assistant. " + LONG,
                      handled_by="battery_support")
        record = TurnRecord(index=1, channel="website", reply=reply, events=tuple(events),
                            customer_texts=("my battery won't charge",), known_data=(), today=date(2026, 9, 29))
        return check_turn(record, Expect(), Who(channel="website", session="sess-ananya"))

    def test_a_reply_that_could_not_be_cut_fails_the_turn(self):
        found = self.failures([{"event": "reply_too_long", "reason": "reference_lost",
                                "words_before": 95, "sentences_before": 7}])
        self.assertIn("the reply ran past one step (95 words, 7 sentences) and was not cut: reference_lost", found)

    def test_a_reply_that_was_cut_does_not(self):
        found = self.failures([{"event": "reply_shortened", "words_before": 95, "sentences_before": 7,
                                "words_after": 14, "sentences_after": 2}])
        self.assertFalse([f for f in found if "one step" in f])


if __name__ == "__main__":
    unittest.main()
