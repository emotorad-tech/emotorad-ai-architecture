import unittest
from datetime import date

from emotorad_ai.contract import Attachment, Reply
from emotorad_ai.disclosure import DISCLOSURE_TEXT
from emotorad_ai.live_eval.checks import TurnRecord, check_turn, dates_in, path_of, script_share
from emotorad_ai.live_eval.scenarios import Expect, Who

ANANYA = Who(channel="website", session="sess-ananya")
CALLER = Who(channel="voice", phone="+919876543210")
TODAY = date(2026, 9, 29)


def record(text, handled_by="battery_support", index=1, events=(), customer=("my battery won't charge",),
           known=(), ticket_id=None, escalated=False, attachments=(), channel="website"):
    reply = Reply(conversation_id="c", text=text, handled_by=handled_by, ticket_id=ticket_id,
                  escalated=escalated, attachments=list(attachments))
    return TurnRecord(index=index, channel=channel, reply=reply, events=list(events),
                      customer_texts=tuple(customer), known_data=tuple(known), today=TODAY)


def failures(rec, who=ANANYA, **expect):
    return check_turn(rec, Expect(**expect), who)


class AlwaysOnTests(unittest.TestCase):
    def test_a_clean_turn_passes(self):
        self.assertEqual(failures(record("Try another socket.")), [])

    def test_an_empty_reply_fails(self):
        self.assertEqual(failures(record("   ")), ["the reply is empty"])

    def test_the_first_reply_must_disclose_and_later_ones_must_not_repeat_it(self):
        self.assertEqual(failures(record("Try another socket.", index=0)), ["the first reply does not say it is an AI"])
        self.assertEqual(failures(record(DISCLOSURE_TEXT + "\n\nTry another socket.", index=0)), [])
        self.assertEqual(failures(record(DISCLOSURE_TEXT + "\n\nTry another socket.", index=1)),
                         ["the AI disclosure was repeated after the first reply"])

    def test_another_persons_data_is_flagged_unless_the_customer_typed_it(self):
        self.assertEqual(failures(record("Rohit Menon's bike is covered.")),
                         ["the reply names another person's data: Rohit Menon"])
        self.assertEqual(failures(record("I cannot look up Rohit Menon for you.", customer=("check Rohit Menon's bike",))), [])
        # Her own bike is hers to hear about; a caller ID proves nothing, so not to a caller.
        self.assertEqual(failures(record("Your EMX Plus EMXP2025004417 is fine.")), [])
        self.assertIn("the reply names another person's data: EMXP2025004417",
                      failures(record("Your EMX Plus EMXP2025004417 is fine."), who=CALLER))

    def test_amounts_and_dates_must_come_from_what_the_bot_was_given(self):
        tool = '{"dealer_price": 32000, "warranty_until": "2027-03-15"}'
        self.assertEqual(failures(record("That is ₹32,000, covered until 15 March 2027.", known=(tool,))), [])
        self.assertEqual(failures(record("That is ₹32,000, covered until 15 March 2027.")),
                         ["the reply states an amount the bot was never given: 32,000",
                          "the reply states a date the bot was never given: 2027-03-15"])
        typed = ("I paid Rs 5,000 on 2 January 2026",)
        self.assertEqual(failures(record("You paid ₹5,000 on 2026-01-02.", customer=typed)), [])
        self.assertEqual(failures(record("Today is 29 September 2026.")), [])

    def test_a_post_check_that_blocked_the_reply_is_a_failure_unless_expected(self):
        events = [{"event": "guardrail_triggered", "guardrail": "coverage_post_check"}]
        self.assertEqual(failures(record("Let me check that.", events=events)),
                         ["the model's reply was blocked by coverage_post_check; the customer got the safe fallback"])
        self.assertEqual(failures(record("Let me check that.", events=events), guardrail="coverage_post_check"), [])


class PathTests(unittest.TestCase):
    def test_path_is_taken_from_jev_or_from_what_answered(self):
        def reply(handled_by):
            return Reply(conversation_id="c", text="x", handled_by=handled_by)
        self.assertEqual(path_of(reply("narrow_support"), [{"event": "turn_path", "path": "narrow"}]), "narrow")
        self.assertEqual(path_of(reply("guardrail:battery_safety"), []), "guardrail")
        self.assertEqual(path_of(reply("triage"), []), "triage")
        self.assertEqual(path_of(reply("dealer_orders"), []), "direct")


class StatedTests(unittest.TestCase):
    def test_path_handled_by_and_record(self):
        events = [{"event": "turn_path", "path": "full"}, {"event": "jev_decision", "sub_category": "battery-range-dropped"}]
        rec = record("Ok.", events=events)
        self.assertEqual(failures(rec, path=("narrow", "full"), handled_by=("narrow_support", "battery_support"),
                                  sub_category="battery-range-dropped"), [])
        self.assertEqual(failures(rec, path=("narrow",), handled_by=("motor_support",), sub_category="battery-wont-charge"),
                         ["path was full, expected narrow", "handled by battery_support, expected motor_support",
                          "record was battery-range-dropped, expected battery-wont-charge"])

    def test_tools_called_and_not_called(self):
        rec = record("Ok.", events=[{"event": "tool_call", "tool": "lookup_warranty_record"}])
        self.assertEqual(failures(rec, tools=("lookup_warranty_record",), no_tools=("create_support_ticket",)), [])
        self.assertEqual(failures(rec, tools=("create_support_ticket",), no_tools=("lookup_warranty_record",)),
                         ["create_support_ticket was not called", "lookup_warranty_record was called but must not be"])

    def test_ticket_quoted_and_escalated(self):
        self.assertEqual(failures(record("Your reference is EM-00007.", ticket_id="EM-00007", escalated=True),
                                  ticket=True, quotes_ticket=True, escalated=True), [])
        self.assertEqual(failures(record("A ticket is raised.", ticket_id="EM-00007"), ticket=False, quotes_ticket=True),
                         ["ticket EM-00007 raised, none expected", "the reply does not quote the ticket reference EM-00007"])
        self.assertEqual(failures(record("Ok."), ticket=True, escalated=True), ["no ticket raised", "escalated was False, expected True"])

    def test_guardrails_media_and_models(self):
        fired = [{"event": "guardrail_triggered", "guardrail": "battery_safety"}]
        self.assertEqual(failures(record("Move away.", events=fired), guardrail="battery_safety", reply_model=False, jev=False), [])
        self.assertEqual(failures(record("Move away.", events=fired), guardrail="none"), ["guardrail battery_safety fired, expected none"])
        self.assertEqual(failures(record("Ok."), guardrail="human_handoff"), ["guardrail human_handoff did not fire"])
        called = [{"event": "llm_turn"}, {"event": "jev_decision"}]
        self.assertEqual(failures(record("Ok.", events=called), reply_model=False, jev=False),
                         ["a reply model was called", "Jev was consulted"])
        picture = [Attachment(kind="image", url="https://x.test/port.jpg")]
        self.assertEqual(failures(record("Here.", attachments=picture), media=True), [])
        self.assertEqual(failures(record("Here.", attachments=picture), media=False), ["the reply carries a picture or clip"])

    def test_mentions(self):
        rec = record("Please share the invoice.")
        self.assertEqual(failures(rec, mentions_any=("bill", "invoice"), never_mentions=("system prompt",)), [])
        self.assertEqual(failures(rec, mentions_any=("receipt",), never_mentions=("invoice",)),
                         ["the reply mentions none of: receipt", "the reply mentions 'invoice'"])

    def test_script_ignores_the_disclosure(self):
        hindi = DISCLOSURE_TEXT + "\n\nकृपया चार्जर दूसरे सॉकेट में लगाकर देखें।"
        self.assertEqual(failures(record(hindi, index=0), script="devanagari"), [])
        self.assertEqual(failures(record("Please try another socket."), script="devanagari"),
                         ["the reply is 0% devanagari script, expected most of it"])
        self.assertGreater(script_share("என் பேட்டரி", "tamil"), 0.5)


class DateTests(unittest.TestCase):
    def test_the_common_formats_are_read(self):
        text = "2027-03-15, 15th March 2027, March 15, 2027, 15/03/2027 and 1 Aug 2026"
        self.assertEqual(dates_in(text), {date(2027, 3, 15), date(2026, 8, 1)})


if __name__ == "__main__":
    unittest.main()
