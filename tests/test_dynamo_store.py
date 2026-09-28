import json
import unittest

from botocore.exceptions import EndpointConnectionError

from emotorad_ai.conversation import ConversationConflict, ConversationState, StoreUnavailable
from emotorad_ai.observability import EventLog
from emotorad_ai.stores.dynamo import DynamoConversationStore
from tests.dynamo_fixture import MotoTable
from tests.store_contract import StoreContract, inbound, reply, summary

NOW = 1_790_000_000


class DynamoStoreTests(MotoTable, StoreContract, unittest.TestCase):
    def make_store(self, **kw):
        return DynamoConversationStore(self.table, client=self.client, now=lambda: NOW, **kw)

    def item(self, pk, sk):
        return self.client.get_item(TableName=self.table, Key={"PK": {"S": pk}, "SK": {"S": sk}})["Item"]

    def test_a_stale_save_is_refused(self):
        store = self.make_store()
        first, second = store.get("c1"), store.get("c1")
        store.save(first)
        with self.assertRaises(ConversationConflict):
            store.save(second)

    def test_two_saves_from_one_load_conflict(self):
        store = self.make_store()
        state = store.get("c1")
        store.save(state)
        stale = ConversationState.from_json(state.to_json())
        stale.version = 0
        with self.assertRaises(ConversationConflict):
            store.save(stale)

    def test_every_item_carries_its_expiry(self):
        store = self.make_store()
        state = store.get("c1")
        state.user_key, state.turns = "PHONE#+919876543210", 1
        store.save(state)
        store.record_turn(state, inbound("hi"), reply("Hello."), summary("c1"))
        self.assertEqual(int(self.item("CONV#c1", "STATE")["expires_at"]["N"]), NOW + 48 * 3600)
        self.assertEqual(int(self.item("CONV#c1", "TURN#00001")["expires_at"]["N"]), NOW + 90 * 86400)
        summary_item = self.client.query(TableName=self.table, KeyConditionExpression="PK = :pk",
                                         ExpressionAttributeValues={":pk": {"S": "USER#PHONE#+919876543210"}})["Items"][0]
        self.assertEqual(int(summary_item["expires_at"]["N"]), NOW + 90 * 86400)

    def test_a_huge_history_is_trimmed_by_whole_turns_and_logged(self):
        log = EventLog(path=None)
        store = self.make_store(log=log, max_state_bytes=20_000)
        state = store.get("c1")
        for i in range(40):
            state.history.append({"role": "user", "content": "turn %d" % i})
            state.history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t%d" % i, "name": "x", "input": {}}]})
            state.history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % i, "content": "x" * 1000}]})
            state.history.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        store.save(state)
        kept = store.get("c1").history
        self.assertLess(len(json.dumps(kept)), 20_000)
        self.assertEqual(kept[0]["role"], "user")
        self.assertIsInstance(kept[0]["content"], str)  # starts at a turn boundary
        uses = {b["id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_use"}
        results = {b["tool_use_id"] for m in kept if isinstance(m["content"], list) for b in m["content"] if b.get("type") == "tool_result"}
        self.assertEqual(uses, results)
        self.assertEqual(kept[-1], {"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        self.assertTrue(any(e["event"] == "history_trimmed" for e in log.events))

    def test_a_network_failure_is_store_unavailable_not_an_empty_state(self):
        class Broken:
            def get_item(self, **kw):
                raise EndpointConnectionError(endpoint_url="https://dynamodb.ap-south-1.amazonaws.com")
        with self.assertRaises(StoreUnavailable):
            DynamoConversationStore(self.table, client=Broken()).get("c1")


if __name__ == "__main__":
    unittest.main()
