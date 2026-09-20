"""Keeping a long conversation from growing without limit.

Nothing trimmed `state.history`, so every turn resent the whole transcript to
the model. A battery diagnosis runs fifteen to twenty turns on top of a 22k
character prompt, so cost grew with the square of the conversation and a long
enough one would have run into the context window with nothing handling it.

The window is cut only at the start of a customer turn. Cutting anywhere else
orphans a `tool_result` from the `tool_use` it answers, which the API rejects —
a conversation that gets long enough would start failing outright instead of
merely costing too much.
"""

import unittest

from emotorad_ai.conversation import HISTORY_TURNS, customer_texts, trim_history


def _turn(n):
    """One customer turn: their message, the model's tool call, the result, the
    model's reply."""
    return [
        {"role": "user", "content": "message %d" % n},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % n, "name": "search_knowledge"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % n}]},
        {"role": "assistant", "content": [{"type": "text", "text": "reply %d" % n}]},
    ]


def _history(turns):
    out = []
    for n in range(turns):
        out.extend(_turn(n))
    return out


class HistoryWindowTests(unittest.TestCase):
    def test_a_short_conversation_is_untouched(self):
        history = _history(3)
        self.assertEqual(trim_history(list(history), 12), history)

    def test_a_long_conversation_is_cut(self):
        self.assertLess(len(trim_history(_history(40), 5)), len(_history(40)))

    def test_it_keeps_the_most_recent_turns(self):
        trimmed = trim_history(_history(40), 5)
        self.assertIn({"role": "user", "content": "message 39"}, trimmed)
        self.assertNotIn({"role": "user", "content": "message 0"}, trimmed)

    def test_it_keeps_exactly_the_requested_number_of_turns(self):
        trimmed = trim_history(_history(40), 5)
        starts = [e for e in trimmed if e["role"] == "user" and isinstance(e["content"], str)]
        self.assertEqual(len(starts), 5)

    def test_it_begins_at_a_customer_turn(self):
        """The first entry must be a real customer message. Beginning on an
        assistant reply or a tool result is what orphans a tool_use."""
        trimmed = trim_history(_history(40), 5)
        self.assertEqual(trimmed[0]["role"], "user")
        self.assertIsInstance(trimmed[0]["content"], str)

    def test_no_tool_result_is_left_without_its_tool_use(self):
        """The property that actually matters. Every tool_result in the window
        must have the tool_use it answers somewhere above it."""
        trimmed = trim_history(_history(40), 5)
        offered = set()
        for entry in trimmed:
            content = entry["content"]
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    offered.add(block["id"])
                if block.get("type") == "tool_result":
                    self.assertIn(block["tool_use_id"], offered)

    def test_an_empty_history_is_fine(self):
        self.assertEqual(trim_history([], 5), [])

    def test_the_default_window_is_sane(self):
        """Long enough to hold a whole battery diagnosis, short enough to bound
        the cost of one that never ends."""
        self.assertGreaterEqual(HISTORY_TURNS, 8)


class TheAgentLoopAppliesTheWindowTests(unittest.TestCase):
    """The window is worth nothing unless the loop actually uses it."""

    def test_a_long_conversation_stops_growing(self):
        from emotorad_ai.config import Settings
        from emotorad_ai.contract import ANONYMOUS, Identity, InboundMessage
        from emotorad_ai.identity import IdentityResolver
        from emotorad_ai.llm import ScriptedClaude, say
        from emotorad_ai.observability import EventLog
        from emotorad_ai.runtime import Runtime
        from emotorad_ai.tools.mocks import build_registry

        registry = build_registry()
        runtime = Runtime(
            settings=Settings(log_path="", log_to_stdout=False),
            registry=registry,
            llm=ScriptedClaude([say("Noted.")] * 80),
            log=EventLog(path=None),
            resolver=IdentityResolver(registry),
        )
        runtime.conversations.get("long").route_to("battery_support")

        for n in range(40):
            runtime.handle(
                InboundMessage(
                    conversation_id="long",
                    persona="customer",
                    identity=Identity(strength=ANONYMOUS, em_aid="aid-1"),
                    channel="website_chat",
                    message_text="turn %d" % n,
                )
            )

        history = runtime.conversations.get("long").history
        starts = [e for e in history if e["role"] == "user" and isinstance(e["content"], str)]
        self.assertLessEqual(len(starts), HISTORY_TURNS + 1)
        self.assertIn("turn 39", str(history))


class TurnsCarryingAPhotoTests(unittest.TestCase):
    """A customer turn with an image is still a customer turn.

    The window finds turn boundaries by looking for a `user` entry whose content
    is a plain string. Once a customer can send a photo, their turn arrives as a
    list of blocks instead, and a boundary test that only knows about strings
    stops seeing it. The window would then cut at the previous turn or not at
    all, and cutting in the wrong place is what strands a `tool_result` above
    the `tool_use` it answers — which the API rejects outright.

    So the boundary is really "a user entry that is not a tool result".
    """

    def _photo_turn(self, n):
        return [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}},
                    {"type": "text", "text": "photo %d" % n},
                ],
            },
            {"role": "assistant", "content": [{"type": "tool_use", "id": "p%d" % n, "name": "search_knowledge"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "p%d" % n}]},
            {"role": "assistant", "content": [{"type": "text", "text": "reply %d" % n}]},
        ]

    def _mixed(self, turns):
        out = []
        for n in range(turns):
            out.extend(_turn(n) if n % 2 else self._photo_turn(n))
        return out

    def test_a_photo_turn_counts_as_a_turn(self):
        trimmed = trim_history(self._mixed(40), 5)
        starts = [
            e for e in trimmed
            if e["role"] == "user"
            and not (isinstance(e["content"], list)
                     and any(b.get("type") == "tool_result" for b in e["content"]))
        ]
        self.assertEqual(len(starts), 5)

    def test_it_never_begins_on_a_tool_result(self):
        """The property that matters. Beginning here orphans a tool_use."""
        first = trim_history(self._mixed(40), 5)[0]
        self.assertEqual(first["role"], "user")
        if isinstance(first["content"], list):
            self.assertFalse(any(b.get("type") == "tool_result" for b in first["content"]))

    def test_every_tool_result_still_has_its_tool_use(self):
        trimmed = trim_history(self._mixed(40), 5)
        offered = set()
        for entry in trimmed:
            content = entry["content"]
            if not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    offered.add(block["id"])
                if block.get("type") == "tool_result":
                    self.assertIn(block["tool_use_id"], offered)


class CustomerTextsTests(unittest.TestCase):
    """The plain text of every customer message, for the address backstop:
    place_replacement_order accepts an address the customer typed in this
    conversation, and it needs their words, not the model's or a tool's."""

    def test_plain_string_turns_are_collected(self):
        history = [
            {"role": "user", "content": "please send it to 9 New Road"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        self.assertEqual(customer_texts(history), ["please send it to 9 New Road"])

    def test_a_photo_with_text_contributes_its_text_block(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {}},
                    {"type": "text", "text": "here is the address: 9 New Road"},
                ],
            }
        ]
        self.assertEqual(customer_texts(history), ["here is the address: 9 New Road"])

    def test_tool_results_are_never_customer_text(self):
        history = [
            {"role": "user", "content": "melted terminal"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
        ]
        self.assertEqual(customer_texts(history), ["melted terminal"])

    def test_an_empty_history_gives_nothing(self):
        self.assertEqual(customer_texts([]), [])


from emotorad_ai.conversation import address_tokens


class AddressTokensTests(unittest.TestCase):
    """The unit the address backstop compares on. Punctuation and case are
    the model's; the words are the customer's."""

    def test_words_and_numbers_survive_punctuation(self):
        self.assertEqual(
            address_tokens("A1102, Park View City 1 - 122018."),
            {"a1102", "park", "view", "city", "1", "122018"},
        )

    def test_empty_is_empty(self):
        self.assertEqual(address_tokens(""), set())
        self.assertEqual(address_tokens(" , . "), set())


if __name__ == "__main__":
    unittest.main()
