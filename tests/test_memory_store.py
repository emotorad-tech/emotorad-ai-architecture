import unittest

from emotorad_ai.conversation import ConversationStore, InMemoryConversationStore
from tests.store_contract import StoreContract


class InMemoryStoreTests(StoreContract, unittest.TestCase):
    def make_store(self):
        return InMemoryConversationStore()

    def test_the_old_name_still_imports(self):
        self.assertIs(ConversationStore, InMemoryConversationStore)

    def test_get_returns_the_same_object_within_a_process(self):
        store = self.make_store()
        self.assertIs(store.get("c1"), store.get("c1"))


if __name__ == "__main__":
    unittest.main()
