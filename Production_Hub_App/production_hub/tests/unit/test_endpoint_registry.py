from __future__ import annotations

import unittest

from production_hub.core.endpoints.models import ActionDefinition, EndpointDefinition
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.core.endpoints.variables import resolve_template


class EndpointRegistryTests(unittest.TestCase):
    def test_route_template_matches_integer_path_param(self) -> None:
        endpoint = EndpointDefinition(
            key="camera_preset",
            name="Camera Preset",
            route="/camera/{preset:int}",
            actions=[ActionDefinition("panasonic.recall_preset", {"preset": "{{preset}}"})],
        )
        matches = EndpointRegistry([endpoint]).matches("/camera/12", "GET")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][1]["preset"], 12)

    def test_route_template_respects_method(self) -> None:
        endpoint = EndpointDefinition(
            key="post_only",
            name="Post Only",
            route="/post-only",
            actions=[ActionDefinition("delay")],
            allowed_methods=["POST"],
        )
        registry = EndpointRegistry([endpoint])
        self.assertEqual(registry.matches("/post-only", "GET"), [])
        self.assertEqual(len(registry.matches("/post-only", "POST")), 1)

    def test_template_resolution_supports_whole_value_and_embedded_text(self) -> None:
        self.assertEqual(resolve_template("{{preset}}", {"preset": 7}), 7)
        self.assertEqual(resolve_template("#R{{preset}}", {"preset": "07"}), "#R07")


if __name__ == "__main__":
    unittest.main()

