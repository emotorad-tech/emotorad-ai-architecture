"""The prompt blocks are persona-level, shared by every sub-agent of that persona.

A second copy would drift, and the copy that drifts is the one that starts
stating coverage it should not. So there is exactly one, in agents/blocks.py.
"""

import unittest

from emotorad_ai.agents import battery_support, blocks, dealer_orders, motor_support


class SharedBlocksTests(unittest.TestCase):
    def test_every_customer_agent_uses_the_one_facts_block(self):
        for module in (battery_support, motor_support):
            self.assertIs(
                module.build_system_prompt.__globals__["_facts_block"], blocks._facts_block, module.__name__
            )
            self.assertIs(
                module.build_system_prompt.__globals__["_context_block"], blocks._context_block
            )
            self.assertIs(module.build_system_prompt.__globals__["_entry_block"], blocks._entry_block)

    def test_the_dealer_agent_uses_the_one_account_block(self):
        self.assertIs(
            dealer_orders.build_system_prompt.__globals__["_account_block"], blocks._account_block
        )


if __name__ == "__main__":
    unittest.main()
