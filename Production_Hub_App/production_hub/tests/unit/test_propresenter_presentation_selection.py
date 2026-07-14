from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from production_hub.core.config.defaults import build_default_endpoints
from production_hub.core.config.models import ProPresenterConfig
from production_hub.core.endpoints.registry import EndpointRegistry
from production_hub.integrations.propresenter.service import ProPresenterService


class ProPresenterPresentationSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = ProPresenterService(ProPresenterConfig())

    async def test_presentation_by_uuid_quotes_the_path_segment(self) -> None:
        payload = {"presentation": {"id": {"uuid": "song/uuid with spaces"}, "groups": []}}
        self.service.client.get_json = AsyncMock(return_value=payload)

        result = await self.service.presentation_by_uuid("song/uuid with spaces")

        self.assertEqual(payload, result)
        self.service.client.get_json.assert_awaited_once_with(
            "/presentation/song%2Fuuid%20with%20spaces"
        )

    async def test_trigger_presentation_slide_targets_uuid_and_index(self) -> None:
        self.service.client.trigger = AsyncMock(return_value=True)

        result = await self.service.trigger_presentation_slide("song/uuid", 7)

        self.assertTrue(result)
        self.service.client.trigger.assert_awaited_once_with(
            "/presentation/song%2Fuuid/7/trigger"
        )

    async def test_trigger_presentation_slide_rejects_negative_index(self) -> None:
        self.service.client.trigger = AsyncMock(return_value=True)

        with self.assertRaisesRegex(ValueError, "nonnegative"):
            await self.service.trigger_presentation_slide("song-uuid", -1)

        self.service.client.trigger.assert_not_awaited()

    def test_default_endpoints_expose_read_and_post_only_trigger(self) -> None:
        endpoints = {endpoint.key: endpoint for endpoint in build_default_endpoints()}
        read_endpoint = endpoints["presentation_by_uuid"]
        trigger_endpoint = endpoints["trigger_presentation_slide"]

        self.assertEqual("/presentation/{uuid}", read_endpoint.route)
        self.assertEqual(["GET"], read_endpoint.allowed_methods)
        self.assertEqual("read", read_endpoint.behavior_mode)
        self.assertEqual("last_action_data", read_endpoint.response.response_type)
        self.assertEqual(["uuid"], [item.name for item in read_endpoint.inputs])

        self.assertEqual("/presentation/{uuid}/{index:int}/trigger", trigger_endpoint.route)
        self.assertEqual(["POST"], trigger_endpoint.allowed_methods)
        self.assertEqual(["uuid", "index"], [item.name for item in trigger_endpoint.inputs])
        self.assertEqual("0", trigger_endpoint.inputs[1].min_value)

    def test_trigger_endpoint_matches_uuid_and_integer_index(self) -> None:
        registry = EndpointRegistry(build_default_endpoints())

        matches = registry.matches("/presentation/song-uuid/12/trigger", "POST")

        self.assertEqual(1, len(matches))
        endpoint, params = matches[0]
        self.assertEqual("trigger_presentation_slide", endpoint.key)
        self.assertEqual({"uuid": "song-uuid", "index": 12}, params)
        self.assertEqual([], registry.matches("/presentation/song-uuid/12/trigger", "GET"))


if __name__ == "__main__":
    unittest.main()
