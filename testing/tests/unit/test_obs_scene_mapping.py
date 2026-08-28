from __future__ import annotations

import unittest

from production_hub.core.config.defaults import build_default_config
from production_hub.integrations.obs.scene_mapping import find_rule, rule_payload, rule_signature, transition_for_scene


class ObsSceneMappingTests(unittest.TestCase):
    def test_default_look_rule_payload_is_seeded_exactly(self) -> None:
        config = build_default_config().integrations.obs
        rule = find_rule(config, "Presentation Picture-in-Picture")
        self.assertIsNotNone(rule)
        payload = rule_payload(rule)  # type: ignore[arg-type]
        self.assertEqual(payload["show"], [72, 77, 81])
        self.assertEqual(payload["hide"], [73, 78, 79, 75, 74, 76, 82])
        self.assertIn("Presentation Picture-in-Picture", rule_signature("Presentation Picture-in-Picture", payload))

    def test_transition_policy_uses_special_transition_for_special_scene(self) -> None:
        config = build_default_config().integrations.obs
        self.assertEqual(transition_for_scene(config, "Stream Start", ""), ("Old Film Logo", None))
        self.assertEqual(transition_for_scene(config, "ProPresenter Input", "PTZ Camera"), ("Fade", None))


if __name__ == "__main__":
    unittest.main()

