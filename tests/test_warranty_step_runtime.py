"""The warranty step through runtime.handle() (spec 2026-10-09 warranty step)."""

import json
import unittest
from datetime import date

from emotorad_ai.adapters import WebsiteChatAdapter
from emotorad_ai.agents.battery_support import AGENT_NAME as BATTERY
from emotorad_ai.config import Settings
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import TURN_FACT_FIELDS, Runtime
from emotorad_ai.tools import fixtures
from emotorad_ai.tools.mocks import LOOKUP_WARRANTY_RECORD, build_registry
from emotorad_ai.tools.oms_db import to_record

TODAY = date(2026, 10, 9)
PHONE = fixtures.SESSIONS["sess-ananya"]


def record(frame, bought=None, invoice=False):
    row = {"frame_number": frame, "product_name": "T-Rex Air", "purchase_date": bought,
           "invoice_image": "f-%s" % frame if invoice else None, "status": "active"}
    return to_record(row, TODAY, invoice_state=lambda file_id: "readable" if file_id else "unreadable")


DATED = record("EMXP0001", bought=date(2025, 3, 12))
ON_FILE = record("EMXP0002", invoice=True)
NO_INVOICE = record("EMXP0003")


class FakeInvoice:
    def __init__(self):
        self.reads = []

    def start(self, jobs):
        for job in jobs:
            job()

    def read_from_oms(self, conversation_id, user_key, cluster_id, phone, frame_number):
        self.reads.append(frame_number)

    def today(self):
        return TODAY


def make(responses, records=(DATED,), step=True, invoice=None, evidence_check=False, **extra):
    registry = build_registry(today=TODAY, warranty_source=lambda phone: list(records) if records else None)
    llm = ScriptedClaude(responses)
    runtime = Runtime(settings=Settings(log_path="", log_to_stdout=False), registry=registry, llm=llm,
                      log=EventLog(path=None), resolver=IdentityResolver(registry), self_service_identity=True,
                      warranty_step=step, invoice=invoice, evidence_check=evidence_check, **extra)
    return runtime, WebsiteChatAdapter(runtime.resolver), llm


def send(runtime, adapter, text, frame="EMXP0001", seen=False, cid="conv-ws"):
    state = runtime.conversations.get(cid)
    state.route_to(BATTERY)
    if frame:
        state.selected_frame = frame
    if seen:
        state.evidence_seen = True
    return runtime.handle(adapter.to_message({"conversation_id": cid, "session_token": "sess-ananya", "text": text}))


class HiddenTests(unittest.TestCase):
    def test_the_state_field_is_a_turn_fact(self):
        self.assertIn("warranty_step_frames", TURN_FACT_FIELDS)

    def test_no_cover_in_the_prompt_before_the_step(self):
        runtime, adapter, llm = make([say("Let's check the charger first.")])
        send(runtime, adapter, "my battery won't charge")
        system = llm.requests[0]["system"]
        self.assertIn("EMXP0001", system)
        self.assertIn("CHECKED AFTER THE FAULT IS CONFIRMED", system)
        # The prompt's fixed text speaks of warranty in general; the bike's
        # own cover line (per part, with dates) must be absent.
        self.assertNotIn("Per part:", system)
        self.assertNotIn("2026-03-11", system)

    def test_the_lookup_answers_after_issue_and_sets_no_coverage_result(self):
        runtime, adapter, llm = make([call_tool(LOOKUP_WARRANTY_RECORD, {}), say("Let's check the charger.")])
        send(runtime, adapter, "is my battery covered?")
        result = json.dumps(llm.requests[1]["messages"][-1], default=str)
        self.assertIn("warranty_after_issue", result)
        self.assertIsNone(runtime.conversations.get("conv-ws").coverage_result)

    def test_with_the_switch_off_everything_is_as_today(self):
        runtime, adapter, llm = make([call_tool(LOOKUP_WARRANTY_RECORD, {}), say("You're covered.")], step=False)
        send(runtime, adapter, "is my battery covered?")
        self.assertIn("from_warranty_api", json.dumps(runtime.conversations.get("conv-ws").coverage_result))


class StepTests(unittest.TestCase):
    def test_nothing_happens_before_the_issue_is_verified(self):
        runtime, adapter, _ = make([say("Let's check the charger.")])
        reply = send(runtime, adapter, "my battery won't charge")
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])
        self.assertEqual(reply.actions, [])

    def test_dated_sets_the_coverage_result_and_appends_nothing(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault in your video.")])
        reply = send(runtime, adapter, "here is the video", seen=True)
        state = runtime.conversations.get("conv-ws")
        self.assertEqual(state.warranty_step_frames, ["EMXP0001"])
        self.assertIn("from_warranty_api", json.dumps(state.coverage_result))
        self.assertIn("Thanks, I can see the fault", reply.text)
        self.assertNotIn("invoice", reply.text.lower())

    def test_invoice_on_file_starts_the_read_and_says_so(self):
        invoice = FakeInvoice()
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(ON_FILE,), invoice=invoice)
        reply = send(runtime, adapter, "here is the video", frame="EMXP0002", seen=True)
        self.assertEqual(invoice.reads, ["EMXP0002"])
        self.assertIn("I'm checking the invoice we have on file for your bike.", reply.text)

    def test_invoice_on_file_without_an_invoice_service_still_says_so(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(ON_FILE,), invoice=None)
        reply = send(runtime, adapter, "here is the video", frame="EMXP0002", seen=True)
        self.assertIn("I'm checking the invoice", reply.text)

    def test_no_invoice_asks_for_one(self):
        runtime, adapter, _ = make([say("Thanks, I can see the fault.")], records=(NO_INVOICE,))
        reply = send(runtime, adapter, "here is the video", frame="EMXP0003", seen=True)
        self.assertIn("please send a clear photo or PDF of your purchase invoice", reply.text)

    def test_once_per_bike(self):
        runtime, adapter, _ = make([say("Thanks."), say("Anything else?")], records=(NO_INVOICE,))
        send(runtime, adapter, "here is the video", frame="EMXP0003", seen=True)
        again = send(runtime, adapter, "ok", frame="EMXP0003", seen=True)
        self.assertNotIn("purchase invoice", again.text)

    def test_a_rider_with_two_bikes_and_none_chosen_waits(self):
        runtime, adapter, _ = make([say("Which bike is it?")], records=(DATED, NO_INVOICE))
        reply = send(runtime, adapter, "here is the video", frame=None, seen=True)
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])
        self.assertNotIn("invoice", reply.text.lower())

    def test_a_hazard_reply_never_runs_the_step(self):
        runtime, adapter, _ = make([say("Please stop using the battery, it could catch fire.")],
                                   records=(NO_INVOICE,))
        send(runtime, adapter, "it smells odd", frame="EMXP0003", seen=True)
        self.assertEqual(runtime.conversations.get("conv-ws").warranty_step_frames, [])

    def test_with_the_evidence_check_on_only_a_passing_verdict_counts(self):
        runtime, _, _ = make([], records=(NO_INVOICE,), evidence_check=True)
        runtime._evidence_gated = lambda state: True  # a fault chat with the check on
        state = runtime.conversations.get("conv-ws")
        state.evidence_seen = True
        self.assertFalse(runtime._issue_verified(state))
        state.evidence_verdict = {"passed": True}
        self.assertTrue(runtime._issue_verified(state))

    def test_with_the_evidence_check_off_a_photo_or_video_counts(self):
        runtime, _, _ = make([], records=(NO_INVOICE,))
        state = runtime.conversations.get("conv-ws")
        self.assertFalse(runtime._issue_verified(state))
        state.evidence_seen = True
        self.assertTrue(runtime._issue_verified(state))

    def test_the_lookup_is_open_after_the_step(self):
        runtime, adapter, llm = make([say("Thanks."), call_tool(LOOKUP_WARRANTY_RECORD, {}), say("Covered.")])
        send(runtime, adapter, "here is the video", seen=True)
        send(runtime, adapter, "am I covered?")
        self.assertNotIn("warranty_after_issue", json.dumps(llm.requests[-1]["messages"][-1], default=str))


if __name__ == "__main__":
    unittest.main()
