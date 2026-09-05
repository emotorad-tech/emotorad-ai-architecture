import unittest

from emotorad_ai.tools.mocks import SEARCH_KNOWLEDGE, build_registry


class SearchKnowledgeTopicsTests(unittest.TestCase):
    def _enum(self, registry):
        schema = registry.schemas_for([SEARCH_KNOWLEDGE])[0]
        return schema["input_schema"]["properties"]["topic"]["enum"]

    def test_the_default_enum_is_the_two_built_in_topics(self):
        self.assertEqual(self._enum(build_registry()), ["battery", "motor"])

    def test_the_enum_follows_the_topics_given(self):
        registry = build_registry(topics=("battery", "motor", "brakes"))
        self.assertEqual(self._enum(registry), ["battery", "motor", "brakes"])


if __name__ == "__main__":
    unittest.main()
