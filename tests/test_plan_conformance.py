"""Conformance with the build plan — the divergences a QC pass found.

Each test here corresponds to something `docs/Emotorad_Platform_Build_Plan.md` or
`docs/Emotorad_HLD_Current.md` states as a requirement, where the code had
quietly stopped matching it. They exist so the drift cannot happen again silently.
"""

import os
import unittest
from datetime import date

from emotorad_ai.adapters import WhatsAppAdapter
from emotorad_ai.config import Settings
from emotorad_ai.contract import Reply
from emotorad_ai.identity import IdentityResolver
from emotorad_ai.llm import ScriptedClaude, call_tool, say
from emotorad_ai.observability import EventLog
from emotorad_ai.runtime import Runtime
from emotorad_ai import media
from emotorad_ai.media import load_catalogue
from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, SEND_GUIDE_MEDIA, build_registry
from tests.test_media import _WithStore

TODAY = date(2026, 8, 6)


def make_runtime(script):
    # The guide-media catalogue is what makes `send_guide_media` exist at all —
    # the registry skips it when there are no pictures, so an agent with none is
    # never told it can send one.
    registry = build_registry(today=TODAY, guide_media=load_catalogue(), sent_media={})
    llm = ScriptedClaude(script)
    runtime = Runtime(
        settings=Settings(log_to_stdout=False, log_path=None),
        registry=registry, llm=llm,
        log=EventLog(path=None, to_stdout=False),
        resolver=IdentityResolver(registry),
    )
    return runtime, llm


def whatsapp(runtime, text, sender="919876543210", conversation_id="c1"):
    return runtime.handle(
        WhatsAppAdapter(runtime.resolver).to_message(
            {"from": sender, "text": text, "conversation_id": conversation_id}
        )
    )


class EnrichmentReachesTheModelTests(unittest.TestCase):
    """§3.2.1 — enrichment was computed, logged, and then thrown away."""

    def test_the_enriched_block_is_actually_in_the_system_prompt(self):
        runtime, llm = make_runtime([say("Let me help.")])
        # Give this cluster some browsing to summarise.
        runtime.enricher_events = None
        original = runtime.enricher.build

        def with_events(resolved, **kwargs):
            return original(
                resolved,
                events=[{"properties": {"model": "EMX Plus"}}] * 2,
                signals=["emi_page_viewed"],
            )

        runtime.enricher.build = with_events
        whatsapp(runtime, "battery won't charge")

        prompt = llm.requests[0]["system"]
        self.assertIn("Recently viewed", prompt)
        self.assertIn("EMI options", prompt)

    def test_it_is_built_once_per_conversation_not_once_per_turn(self):
        # Rebuilding it every turn costs queries, moves the block in the prompt
        # (defeating prefix caching), and cannot change the answer.
        runtime, llm = make_runtime([say("First."), say("Second."), say("Third.")])
        calls = []
        original = runtime.enricher.build
        runtime.enricher.build = lambda *a, **k: (calls.append(1), original(*a, **k))[1]

        for _ in range(3):
            whatsapp(runtime, "still not charging")

        self.assertEqual(len(calls), 1, "enrichment rebuilt per turn")

    def test_enrichment_never_authorises_an_ownership_claim(self):
        # It may personalise; only the warranty record may authorise.
        runtime, llm = make_runtime([say("Let me help.")])
        whatsapp(runtime, "battery won't charge")
        prompt = llm.requests[0]["system"]
        self.assertIn("never treat as proof of ownership or coverage", prompt)


class ReplyAttachmentsTests(unittest.TestCase):
    """§3.1 — 'the reply shape needs its own attachments field'."""

    def setUp(self):
        # Catalogue pictures are S3 asset ids, so without a media store there is
        # no URL to send and `send_guide_media` refuses rather than claiming it
        # sent something. That refusal is its own behaviour, tested in
        # test_media.py; here we want the configured case, so a fake store
        # stands in for S3 without touching AWS.
        self._previous = os.environ.get(media.CLOUD_NAME_ENV)
        os.environ[media.CLOUD_NAME_ENV] = "testcloud"
        self._store_ctx = _WithStore()
        self._store_ctx.__enter__()

    def tearDown(self):
        self._store_ctx.__exit__(None, None, None)
        if self._previous is None:
            os.environ.pop(media.CLOUD_NAME_ENV, None)
        else:
            os.environ[media.CLOUD_NAME_ENV] = self._previous

    def test_the_reply_contract_carries_attachments(self):
        self.assertIn("attachments", Reply("c1", "hi", "agent").to_dict())

    def test_a_picture_the_model_asked_for_reaches_the_customer(self):
        # §3.1's requirement is that an outbound picture *can* reach a customer,
        # and it still does. What changed is what decides: the model asks for one
        # by catalogue key. It cannot invent a URL — the key is an enum in the
        # schema and code resolves the address.
        runtime, _ = make_runtime([
            call_tool(SEND_GUIDE_MEDIA, {"key": "soc_button"}, "t1"),
            say("Press and hold the SOC button on the side of the pack."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertTrue(reply.attachments)
        url = reply.attachments[0].url
        self.assertTrue(url.startswith("https://signed.test/"), url)
        self.assertIn("assets/afs/battery/", url)

    def test_retrieving_a_record_does_not_attach_its_media(self):
        """Retrieval informs the model; it does not send pictures to the customer.

        This was the other way round until the model had a way to ask (auto-attach
        2026-08-06, `send_guide_media` 2026-09-08, both live for five weeks after).
        Inferring from retrieval put the revival clip in front of customers still
        on the SOC-button step, and re-sent photos they already had, because it
        keys off what a search returned rather than what the reply is about.
        """
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging", "topic": "battery"}, "t1"),
            say("Check the charger is fully seated."),
        ])
        self.assertEqual(whatsapp(runtime, "battery not charging").attachments, [])

    def test_a_reply_with_no_media_carries_an_empty_list_not_none(self):
        runtime, _ = make_runtime([say("Try a different socket.")])
        self.assertEqual(whatsapp(runtime, "battery won't charge").attachments, [])

    def test_the_same_picture_is_not_sent_twice_in_one_conversation(self):
        # The tool refuses the repeat rather than the loop de-duplicating it, so
        # the model is told it already sent this and can stop restating the step.
        runtime, _ = make_runtime([
            call_tool(SEND_GUIDE_MEDIA, {"key": "soc_button"}, "t1"),
            call_tool(SEND_GUIDE_MEDIA, {"key": "soc_button"}, "t2"),
            say("Tell me what you see."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        urls = [a.url for a in reply.attachments]
        self.assertEqual(len(urls), 1, urls)

    def test_a_clip_is_not_announced_to_the_channel_as_a_photo(self):
        # kind used to be hardcoded "image", so a video rendered as a broken
        # picture on every channel that trusts the field.
        runtime, _ = make_runtime([
            call_tool(SEND_GUIDE_MEDIA, {"key": "battery_revival"}, "t1"),
            say("This clip shows the revival process."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertEqual([a.kind for a in reply.attachments], ["video"])


class EmptyReplyTests(unittest.TestCase):
    """A turn that writes nothing must not reach the customer as nothing.

    Seen in a real session: the model called `search_knowledge` and
    `send_guide_media`, got both results, and ended the turn having written no
    text — `stop_reason` `end_turn`, nothing truncated. The customer was sent a
    guide photo and then an empty message. Nothing anywhere checked, because the
    loop's only question was whether the model wanted more tools.
    """

    def test_an_empty_final_turn_hands_over_instead_of_sending_nothing(self):
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            say(""),  # tools came back, model wrote nothing
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertTrue(reply.text.strip(), "customer received an empty message")
        self.assertTrue(reply.escalated)
        self.assertIn("pass you to someone", reply.text)

    def test_whitespace_only_counts_as_empty(self):
        runtime, _ = make_runtime([say("   \n  ")])
        self.assertIn("pass you to someone", whatsapp(runtime, "battery not charging").text)

    def test_it_is_logged_so_the_frequency_can_be_measured(self):
        # How often this happens is what decides whether it is worth recovering
        # from with another round trip rather than handing over.
        runtime, _ = make_runtime([say("")])
        whatsapp(runtime, "battery not charging")
        self.assertTrue(any(e["event"] == "empty_reply" for e in runtime.log.events))


class StuckAgentTests(unittest.TestCase):
    """HLD §6 — 'duplicate tool calls break the loop early'. Only the cap existed."""

    def test_the_same_call_twice_breaks_the_loop_and_hands_over(self):
        runtime, llm = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t2"),  # identical
            say("never reached"),
        ])
        reply = whatsapp(runtime, "battery not charging")

        self.assertTrue(reply.escalated)
        self.assertIn("pass you to someone", reply.text)
        self.assertLess(len(llm.requests), 3, "should not burn the full iteration budget")

    def test_it_is_logged_so_the_loop_can_be_diagnosed(self):
        runtime, _ = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "x"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "x"}, "t2"),
            say("never reached"),
        ])
        whatsapp(runtime, "battery not charging")
        self.assertTrue(any(e["event"] == "stuck_agent" for e in runtime.log.events))

    def test_different_arguments_are_not_treated_as_stuck(self):
        runtime, llm = make_runtime([
            call_tool(SEARCH_KNOWLEDGE, {"query": "not charging"}, "t1"),
            call_tool(SEARCH_KNOWLEDGE, {"query": "charger light off"}, "t2"),
            say("Try a different socket."),
        ])
        reply = whatsapp(runtime, "battery not charging")
        self.assertFalse(reply.escalated)


if __name__ == "__main__":
    unittest.main()
