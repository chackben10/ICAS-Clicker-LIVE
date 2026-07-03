from __future__ import annotations

import unittest

from production_hub.core.automation.cooldowns import CooldownGate, DebounceGate


class AutomationGateTests(unittest.TestCase):
    def test_cooldown_gate(self) -> None:
        gate = CooldownGate(2)
        self.assertTrue(gate.ready(now=10))
        gate.mark(now=10)
        self.assertFalse(gate.ready(now=11))
        self.assertTrue(gate.ready(now=12))

    def test_debounce_gate(self) -> None:
        gate = DebounceGate(0.5)
        self.assertFalse(gate.offer("a", now=1.0))
        self.assertFalse(gate.offer("a", now=1.2))
        self.assertTrue(gate.offer("a", now=1.6))
        gate.mark_applied()
        self.assertFalse(gate.offer("a", now=2.0))


if __name__ == "__main__":
    unittest.main()

